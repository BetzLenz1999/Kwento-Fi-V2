// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IGoodMarketRaffleToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
}

/**
 * @title GoodMarketRaffle
 * @notice Round-based G$ raffle pool with tiered prizes.
 *
 * Flow:
 * - A wallet joins the open round by approving and depositing exactly 500 G$.
 * - Each round accepts exactly 10 unique participants.
 * - A participant may refundEntry() only while the round is still Open, so the
 *   pool can never be drained after the round fills and the draw commits.
 * - Once full, deposits and refunds are blocked until the randomness provider
 *   finalizes winners.
 * - Four unique winners receive tiered claimable rewards (40/30/20/10).
 * - Winners pull rewards with withdrawReward(), so distribution cannot be
 *   blocked by one wallet.
 *
 * Solvency:
 * - The constructor requires sum(prizeTiers) == ENTRY_FEE * MAX_PARTICIPANTS, so
 *   a deployment whose prizes do not exactly equal the pool cannot exist. The
 *   contract therefore always holds enough G$ to pay every winner, with no
 *   external funding and no house edge.
 *
 * Randomness:
 * - This contract intentionally does not use block.timestamp/blockhash as the
 *   source of truth.
 * - Deploy with a trusted/verifiable randomness provider wallet or coordinator.
 * - The provider must call drawWinners(seed) after a round is full.
 * - NOTE: the provider chooses the seed, so the provider can influence the
 *   outcome. Use a non-incentivized key, or a VRF/commit-reveal source.
 */
contract GoodMarketRaffle {
    enum RoundStatus {
        Open,
        Drawing,
        Completed
    }

    uint256 public constant ENTRY_FEE = 500 ether;
    uint16 public constant MAX_PARTICIPANTS = 10;
    uint8 public constant WINNER_COUNT = 4;
    uint256 public constant PRIZE_POOL = 5_000 ether;

    IGoodMarketRaffleToken public immutable gdToken;
    address public immutable randomnessProvider;
    uint256 public currentRoundId;

    /// Prize per finishing position, highest first. Fixed at construction.
    uint256[] public prizeTiers;

    struct Round {
        RoundStatus status;
        address[] participants;
        address[] winners;
        bytes32 randomnessSeed;
        uint256 openedAt;
        uint256 completedAt;
    }

    mapping(uint256 => Round) private rounds;
    mapping(uint256 => mapping(address => bool)) public hasJoined;
    mapping(uint256 => mapping(address => bool)) public isWinner;
    mapping(uint256 => mapping(address => uint256)) public claimableReward;
    mapping(uint256 => mapping(address => bool)) public rewardClaimed;

    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _status = _NOT_ENTERED;

    modifier nonReentrant() {
        require(_status != _ENTERED, "reentrant_call");
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }

    event RoundOpened(uint256 indexed roundId, uint256 openedAt);
    event RaffleJoined(uint256 indexed roundId, address indexed participant, uint256 amount, uint256 participantCount);
    event EntryRefunded(uint256 indexed roundId, address indexed participant, uint256 amount);
    event RoundReadyForDraw(uint256 indexed roundId, uint256 participantCount);
    event WinnersDrawn(uint256 indexed roundId, bytes32 indexed randomnessSeed, address[] winners, uint256[] prizes);
    event RewardWithdrawn(uint256 indexed roundId, address indexed winner, uint256 amount);

    modifier onlyRandomnessProvider() {
        require(msg.sender == randomnessProvider, "not_randomness_provider");
        _;
    }

    /**
     * @param gdTokenAddress G$ token on the deployment chain.
     * @param trustedRandomnessProvider Wallet allowed to submit the draw seed.
     * @param tiers Prize per position, highest first. Must sum to the prize pool.
     */
    constructor(address gdTokenAddress, address trustedRandomnessProvider, uint256[] memory tiers) {
        require(gdTokenAddress != address(0), "zero_gd_token");
        require(trustedRandomnessProvider != address(0), "zero_randomness_provider");
        require(tiers.length == WINNER_COUNT, "tier_count_mismatch");
        require(WINNER_COUNT <= MAX_PARTICIPANTS, "winners_exceed_participants");
        require(PRIZE_POOL == ENTRY_FEE * MAX_PARTICIPANTS, "pool_constant_mismatch");

        uint256 total;
        for (uint256 i = 0; i < tiers.length; i++) {
            require(tiers[i] > 0, "zero_tier");
            total += tiers[i];
        }
        require(total == PRIZE_POOL, "pool_mismatch");

        gdToken = IGoodMarketRaffleToken(gdTokenAddress);
        randomnessProvider = trustedRandomnessProvider;
        prizeTiers = tiers;

        currentRoundId = 1;
        rounds[currentRoundId].status = RoundStatus.Open;
        rounds[currentRoundId].openedAt = block.timestamp;
        emit RoundOpened(currentRoundId, block.timestamp);
    }

    function joinRaffle() external nonReentrant returns (bool) {
        Round storage round = rounds[currentRoundId];
        require(round.status == RoundStatus.Open, "round_not_open");
        require(round.participants.length < MAX_PARTICIPANTS, "round_full");
        require(!hasJoined[currentRoundId][msg.sender], "already_joined");

        bool ok = gdToken.transferFrom(msg.sender, address(this), ENTRY_FEE);
        require(ok, "gd_transferfrom_failed");

        hasJoined[currentRoundId][msg.sender] = true;
        round.participants.push(msg.sender);

        emit RaffleJoined(currentRoundId, msg.sender, ENTRY_FEE, round.participants.length);

        if (round.participants.length == MAX_PARTICIPANTS) {
            round.status = RoundStatus.Drawing;
            emit RoundReadyForDraw(currentRoundId, MAX_PARTICIPANTS);
        }

        return true;
    }

    /**
     * @notice Leave an open round and get the entry fee back.
     * @dev Only possible while the round is Open. Once the round fills the
     * status becomes Drawing and refunds close permanently, so a committed draw
     * can never be under-funded. A refund before the round fills simply frees
     * the slot for someone else.
     */
    function refundEntry() external nonReentrant returns (bool) {
        Round storage round = rounds[currentRoundId];
        require(round.status == RoundStatus.Open, "round_closed");
        require(hasJoined[currentRoundId][msg.sender], "not_joined");

        hasJoined[currentRoundId][msg.sender] = false;
        _removeParticipant(round, msg.sender);

        bool ok = gdToken.transfer(msg.sender, ENTRY_FEE);
        require(ok, "gd_transfer_failed");
        emit EntryRefunded(currentRoundId, msg.sender, ENTRY_FEE);
        return true;
    }

    function drawWinners(bytes32 seed) external onlyRandomnessProvider returns (address[] memory winners) {
        Round storage round = rounds[currentRoundId];
        require(round.status == RoundStatus.Drawing, "round_not_ready");
        require(round.participants.length == MAX_PARTICIPANTS, "participants_incomplete");
        require(seed != bytes32(0), "zero_seed");

        // Partial Fisher-Yates over the slot indexes. Drawing uniformly from the
        // remaining slice removes the upward bias a linear "idx + 1" collision
        // probe would introduce (which favoured the wallets that joined last).
        uint16 n = MAX_PARTICIPANTS;
        uint16[] memory pool = new uint16[](n);
        for (uint16 i = 0; i < n; i++) {
            pool[i] = i;
        }

        winners = new address[](WINNER_COUNT);
        uint256[] memory prizes = new uint256[](WINNER_COUNT);

        for (uint16 i = 0; i < WINNER_COUNT; i++) {
            uint16 j = i + uint16(uint256(keccak256(abi.encode(seed, currentRoundId, i))) % (n - i));
            (pool[i], pool[j]) = (pool[j], pool[i]);

            address winner = round.participants[pool[i]];
            winners[i] = winner;
            prizes[i] = prizeTiers[i];

            round.winners.push(winner);
            isWinner[currentRoundId][winner] = true;
            claimableReward[currentRoundId][winner] = prizeTiers[i];
        }

        round.randomnessSeed = seed;
        round.status = RoundStatus.Completed;
        round.completedAt = block.timestamp;
        emit WinnersDrawn(currentRoundId, seed, winners, prizes);

        _openNextRound();
    }

    function withdrawReward(uint256 roundId) external nonReentrant returns (bool) {
        uint256 amount = claimableReward[roundId][msg.sender];
        require(amount > 0, "no_reward");
        require(!rewardClaimed[roundId][msg.sender], "already_claimed");

        rewardClaimed[roundId][msg.sender] = true;
        claimableReward[roundId][msg.sender] = 0;

        bool ok = gdToken.transfer(msg.sender, amount);
        require(ok, "gd_transfer_failed");
        emit RewardWithdrawn(roundId, msg.sender, amount);
        return true;
    }

    function getRound(uint256 roundId) external view returns (
        RoundStatus status,
        uint256 participantCount,
        uint256 winnerCount,
        bytes32 randomnessSeed,
        uint256 openedAt,
        uint256 completedAt
    ) {
        Round storage round = rounds[roundId];
        return (
            round.status,
            round.participants.length,
            round.winners.length,
            round.randomnessSeed,
            round.openedAt,
            round.completedAt
        );
    }

    function getParticipants(uint256 roundId) external view returns (address[] memory) {
        return rounds[roundId].participants;
    }

    function getWinners(uint256 roundId) external view returns (address[] memory) {
        return rounds[roundId].winners;
    }

    /// @notice The full prize table, highest position first.
    function getPrizeTiers() external view returns (uint256[] memory) {
        return prizeTiers;
    }

    function contractBalance() external view returns (uint256) {
        return gdToken.balanceOf(address(this));
    }

    function _removeParticipant(Round storage round, address participant) internal {
        uint256 len = round.participants.length;
        for (uint256 i = 0; i < len; i++) {
            if (round.participants[i] == participant) {
                round.participants[i] = round.participants[len - 1];
                round.participants.pop();
                return;
            }
        }
    }

    function _openNextRound() internal {
        currentRoundId += 1;
        rounds[currentRoundId].status = RoundStatus.Open;
        rounds[currentRoundId].openedAt = block.timestamp;
        emit RoundOpened(currentRoundId, block.timestamp);
    }
}
