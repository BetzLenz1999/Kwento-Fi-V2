// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @notice A simple Celo Garden: seed principal is returned; bonuses only use sponsor funds.
/// @dev No owner, admin sweep, reserve buffer, bonus debt, or upgrade path.
contract KwentofiGarden is ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 public constant SEED_PRICE = 10 ether; // 10 G$ per seed (18-decimal G$ token)
    uint256 public constant MIN_SEEDS = 1;
    uint256 public constant MAX_SEEDS = 100;
    uint256 public constant GROWTH_PERIOD = 1 days;
    uint256 public constant MAX_BONUS_BPS = 500; // Up to 5% of principal, once per completed garden.
    uint256 private constant BPS_DENOMINATOR = 10_000;

    IERC20 public immutable goodDollarToken;

    struct Garden {
        uint256 principal;
        uint64 plantedAt;
        uint16 seedCount;
        bool active;
    }

    mapping(address => Garden) public gardens;

    /// @notice Principal owed to active gardens; never available for rewards.
    uint256 public totalPrincipalLiability;
    /// @notice Donations expressly assigned to optional bonuses.
    uint256 public sponsorRewardPool;
    uint256 public totalSponsorFunding;
    uint256 public totalSponsorBonusesPaid;

    error InvalidToken();
    error UnsupportedTokenDecimals(uint8 actualDecimals);
    error InvalidSeedCount(uint256 count);
    error GardenAlreadyActive();
    error NoActiveGarden();
    error GardenNotMature(uint256 maturityAt);
    error GardenAlreadyMature(uint256 maturityAt);
    error InvalidSponsorAmount();
    error UnexpectedTokenAmount(uint256 expected, uint256 received);
    error InsolventAccounting();

    event GardenPlanted(address indexed gardener, uint256 seedCount, uint256 principal, uint256 plantedAt, uint256 maturityAt);
    event GardenHarvested(address indexed gardener, uint256 principalReturned, uint256 sponsorBonus, uint256 harvestedAt);
    event GardenExited(address indexed gardener, uint256 principalReturned, uint256 exitedAt);
    event SponsorPoolFunded(address indexed sponsor, uint256 amount, uint256 poolBalance);

    constructor(address tokenAddress) {
        if (tokenAddress == address(0) || tokenAddress.code.length == 0) revert InvalidToken();
        uint8 tokenDecimals = IERC20Metadata(tokenAddress).decimals();
        if (tokenDecimals != 18) revert UnsupportedTokenDecimals(tokenDecimals);
        goodDollarToken = IERC20(tokenAddress);
    }

    /// @notice Buy 1–100 seeds; the purchase automatically plants one 24-hour garden.
    function buyAndPlant(uint256 seedCount) external nonReentrant {
        if (seedCount < MIN_SEEDS || seedCount > MAX_SEEDS) revert InvalidSeedCount(seedCount);
        if (gardens[msg.sender].active) revert GardenAlreadyActive();

        uint256 principal = seedCount * SEED_PRICE;
        uint256 balanceBefore = goodDollarToken.balanceOf(address(this));
        goodDollarToken.safeTransferFrom(msg.sender, address(this), principal);
        uint256 received = goodDollarToken.balanceOf(address(this)) - balanceBefore;
        if (received != principal) revert UnexpectedTokenAmount(principal, received);

        uint64 plantedAt = uint64(block.timestamp);
        gardens[msg.sender] = Garden({
            principal: principal,
            plantedAt: plantedAt,
            seedCount: uint16(seedCount),
            active: true
        });
        totalPrincipalLiability += principal;
        _assertSolvent();

        emit GardenPlanted(msg.sender, seedCount, principal, plantedAt, uint256(plantedAt) + GROWTH_PERIOD);
    }

    /// @notice Mature harvest returns all principal and up to 5% from the sponsor pool.
    /// @dev A depleted pool yields a zero bonus and never creates a debt or blocks principal return.
    function harvestAndWithdraw() external nonReentrant returns (uint256 principalReturned, uint256 sponsorBonus) {
        Garden memory garden = gardens[msg.sender];
        if (!garden.active) revert NoActiveGarden();
        uint256 gardenMaturityAt = uint256(garden.plantedAt) + GROWTH_PERIOD;
        if (block.timestamp < gardenMaturityAt) revert GardenNotMature(gardenMaturityAt);

        principalReturned = garden.principal;
        uint256 bonusCap = (principalReturned * MAX_BONUS_BPS) / BPS_DENOMINATOR;
        sponsorBonus = bonusCap < sponsorRewardPool ? bonusCap : sponsorRewardPool;

        delete gardens[msg.sender];
        totalPrincipalLiability -= principalReturned;
        sponsorRewardPool -= sponsorBonus;
        totalSponsorBonusesPaid += sponsorBonus;
        _assertSolvent();

        goodDollarToken.safeTransfer(msg.sender, principalReturned + sponsorBonus);
        emit GardenHarvested(msg.sender, principalReturned, sponsorBonus, block.timestamp);
    }

    /// @notice Exit before maturity: return all principal, with no sponsor bonus.
    function exitGarden() external nonReentrant returns (uint256 principalReturned) {
        Garden memory garden = gardens[msg.sender];
        if (!garden.active) revert NoActiveGarden();
        uint256 gardenMaturityAt = uint256(garden.plantedAt) + GROWTH_PERIOD;
        if (block.timestamp >= gardenMaturityAt) revert GardenAlreadyMature(gardenMaturityAt);

        principalReturned = garden.principal;
        delete gardens[msg.sender];
        totalPrincipalLiability -= principalReturned;
        _assertSolvent();
        goodDollarToken.safeTransfer(msg.sender, principalReturned);
        emit GardenExited(msg.sender, principalReturned, block.timestamp);
    }

    /// @notice Irrevocably contribute G$ to the optional sponsor bonus pool.
    function fundSponsorPool(uint256 amount) external nonReentrant {
        if (amount == 0) revert InvalidSponsorAmount();
        uint256 balanceBefore = goodDollarToken.balanceOf(address(this));
        goodDollarToken.safeTransferFrom(msg.sender, address(this), amount);
        uint256 received = goodDollarToken.balanceOf(address(this)) - balanceBefore;
        if (received != amount) revert UnexpectedTokenAmount(amount, received);
        sponsorRewardPool += amount;
        totalSponsorFunding += amount;
        _assertSolvent();
        emit SponsorPoolFunded(msg.sender, amount, sponsorRewardPool);
    }

    function maturityAt(address gardener) external view returns (uint256) {
        Garden storage garden = gardens[gardener];
        return garden.active ? uint256(garden.plantedAt) + GROWTH_PERIOD : 0;
    }

    function maximumBonus(address gardener) external view returns (uint256) {
        Garden storage garden = gardens[gardener];
        if (!garden.active) return 0;
        uint256 cap = (garden.principal * MAX_BONUS_BPS) / BPS_DENOMINATOR;
        return cap < sponsorRewardPool ? cap : sponsorRewardPool;
    }

    /// @notice Accounting invariant; unsolicited token transfers are not sponsor funding.
    function isSolvent() public view returns (bool) {
        return goodDollarToken.balanceOf(address(this)) >= totalPrincipalLiability + sponsorRewardPool;
    }

    function _assertSolvent() private view {
        if (!isSolvent()) revert InsolventAccounting();
    }
}
