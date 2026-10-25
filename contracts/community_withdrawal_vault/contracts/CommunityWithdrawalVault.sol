// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title CommunityWithdrawalVault
/// @notice Holds G$ liquidity and pays only a directly-calling member with a
///         valid, project-bound authorization signed by the Community issuer.
/// @dev Members call withdraw themselves and therefore pay their own network
///      gas. There is no relayer, upgrade path, arbitrary-call function, or
///      owner-controlled token sweep. Anyone may fund this vault by transferring
///      the immutable ERC-20 token directly to its address; funding gives no
///      withdrawal entitlement.
contract CommunityWithdrawalVault is EIP712, ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant AUTHORIZATION_TYPEHASH = keccak256(
        "WithdrawalAuthorization(address recipient,uint256 amount,bytes32 withdrawalId,uint256 nonce,uint256 deadline,bytes32 projectId)"
    );
    uint256 public constant MAX_AUTHORIZATION_LIFETIME = 15 minutes;

    struct WithdrawalAuthorization {
        address recipient;
        uint256 amount;
        bytes32 withdrawalId;
        uint256 nonce;
        uint256 deadline;
        bytes32 projectId;
    }

    IERC20 public immutable goodDollarToken;
    address public immutable governance;
    bytes32 public immutable communityProjectId;
    uint256 public immutable expectedChainId;
    uint256 public immutable minimumWithdrawalAmount;
    uint256 public immutable maximumWithdrawalAmount;
    /// @notice UTC calendar-day cap; it is not a rolling 24-hour limit.
    uint256 public immutable dailyOutflowCap;

    /// @notice Immutable Community service signer. Rotation requires a separately
    ///         reviewed migration; governance cannot grant itself payout authority.
    address public immutable issuerSigner;
    bool public paused;

    mapping(address member => uint256 nonce) public nonces;
    mapping(bytes32 withdrawalId => bool processed) public processedWithdrawalIds;
    mapping(uint256 utcDay => uint256 amount) public withdrawnByUtcDay;

    error InvalidAddress();
    error InvalidProjectId();
    error WrongChainId(uint256 expected, uint256 actual);
    error InvalidLimits();
    error UnauthorizedGovernance();
    error AlreadyPaused();
    error NotPaused();
    error VaultIsPaused();
    error NotRecipient();
    error InvalidAmount();
    error InvalidWithdrawalId();
    error InvalidProject();
    error AuthorizationExpired();
    error AuthorizationDeadlineTooFar();
    error InvalidNonce(uint256 expected, uint256 supplied);
    error WithdrawalAlreadyProcessed();
    error InvalidIssuerAuthorization();
    error MaximumWithdrawalExceeded();
    error DailyOutflowLimitExceeded();
    error InsufficientVaultBalance();

    event WithdrawalPaid(
        address indexed recipient,
        uint256 amount,
        bytes32 indexed withdrawalId,
        uint256 nonce,
        uint256 utcDay
    );
    event WithdrawalsPaused(address indexed governance);
    event WithdrawalsUnpaused(address indexed governance);

    modifier onlyGovernance() {
        if (msg.sender != governance) revert UnauthorizedGovernance();
        _;
    }

    constructor(
        address tokenAddress,
        address initialIssuerSigner,
        address governanceAddress,
        bytes32 projectId,
        uint256 requiredChainId,
        uint256 minimumAmount,
        uint256 maximumAmount,
        uint256 dailyCap
    ) EIP712("GoodMarket Community Withdrawal Vault", "1") {
        if (tokenAddress == address(0) || tokenAddress.code.length == 0
            || initialIssuerSigner == address(0) || governanceAddress == address(0)
            || governanceAddress.code.length == 0 || initialIssuerSigner == governanceAddress) {
            revert InvalidAddress();
        }
        if (projectId == bytes32(0)) revert InvalidProjectId();
        if (requiredChainId != block.chainid) {
            revert WrongChainId(requiredChainId, block.chainid);
        }
        if (minimumAmount == 0 || maximumAmount < minimumAmount
            || dailyCap < maximumAmount) {
            revert InvalidLimits();
        }

        goodDollarToken = IERC20(tokenAddress);
        issuerSigner = initialIssuerSigner;
        governance = governanceAddress;
        communityProjectId = projectId;
        expectedChainId = requiredChainId;
        minimumWithdrawalAmount = minimumAmount;
        maximumWithdrawalAmount = maximumAmount;
        dailyOutflowCap = dailyCap;
    }

    /// @notice Withdraw an exact server-authorized amount to the calling wallet.
    /// @dev msg.sender is the recipient and the member's on-chain authorization.
    ///      This prevents a relayer or browser from redirecting the payout.
    ///      The member submits this transaction and pays the network gas.
    function withdraw(
        WithdrawalAuthorization calldata authorization,
        bytes calldata issuerAuthorization
    ) external nonReentrant {
        if (paused) revert VaultIsPaused();
        if (block.chainid != expectedChainId) {
            revert WrongChainId(expectedChainId, block.chainid);
        }
        if (authorization.recipient != msg.sender) revert NotRecipient();
        if (authorization.projectId != communityProjectId) revert InvalidProject();
        if (authorization.amount < minimumWithdrawalAmount) revert InvalidAmount();
        if (authorization.amount > maximumWithdrawalAmount) revert MaximumWithdrawalExceeded();
        if (authorization.withdrawalId == bytes32(0)) revert InvalidWithdrawalId();
        if (authorization.deadline < block.timestamp) revert AuthorizationExpired();
        if (authorization.deadline > block.timestamp + MAX_AUTHORIZATION_LIFETIME) {
            revert AuthorizationDeadlineTooFar();
        }

        uint256 expectedNonce = nonces[msg.sender];
        if (authorization.nonce != expectedNonce) {
            revert InvalidNonce(expectedNonce, authorization.nonce);
        }
        if (processedWithdrawalIds[authorization.withdrawalId]) {
            revert WithdrawalAlreadyProcessed();
        }

        bytes32 structHash = keccak256(abi.encode(
            AUTHORIZATION_TYPEHASH,
            authorization.recipient,
            authorization.amount,
            authorization.withdrawalId,
            authorization.nonce,
            authorization.deadline,
            authorization.projectId
        ));
        bytes32 digest = _hashTypedDataV4(structHash);
        if (!SignatureChecker.isValidSignatureNow(issuerSigner, digest, issuerAuthorization)) {
            revert InvalidIssuerAuthorization();
        }

        uint256 utcDay = block.timestamp / 1 days;
        uint256 newDailyOutflow = withdrawnByUtcDay[utcDay] + authorization.amount;
        if (newDailyOutflow > dailyOutflowCap) revert DailyOutflowLimitExceeded();
        if (goodDollarToken.balanceOf(address(this)) < authorization.amount) {
            revert InsufficientVaultBalance();
        }

        // Effects before the ERC-20 interaction; any failed transfer reverts all
        // state updates atomically. ReentrancyGuard is an additional safeguard.
        nonces[msg.sender] = expectedNonce + 1;
        processedWithdrawalIds[authorization.withdrawalId] = true;
        withdrawnByUtcDay[utcDay] = newDailyOutflow;

        goodDollarToken.safeTransfer(msg.sender, authorization.amount);

        emit WithdrawalPaid(
            msg.sender,
            authorization.amount,
            authorization.withdrawalId,
            authorization.nonce,
            utcDay
        );
    }

    /// @notice Stop new payouts during an incident. Does not block deposits.
    function pause() external onlyGovernance {
        if (paused) revert AlreadyPaused();
        paused = true;
        emit WithdrawalsPaused(msg.sender);
    }

    function unpause() external onlyGovernance {
        if (!paused) revert NotPaused();
        paused = false;
        emit WithdrawalsUnpaused(msg.sender);
    }

    function currentUtcDayOutflow() external view returns (uint256) {
        return withdrawnByUtcDay[block.timestamp / 1 days];
    }

    function currentUtcDayRemaining() external view returns (uint256) {
        uint256 spent = withdrawnByUtcDay[block.timestamp / 1 days];
        return spent >= dailyOutflowCap ? 0 : dailyOutflowCap - spent;
    }
}
