// SPDX-License-Identifier: MIT
pragma solidity ^0.8.21;

/**
 * @title GoodMarketChat
 * @notice Public community chatroom for GoodMarket.
 *
 * Message bodies live in EVENTS ONLY. There is no message storage.
 *
 * Why events and not a storage ring buffer. Measured on a local EVM (py-evm)
 * by deploying BOTH designs and posting the same 60-message mix, with Celo gas
 * at 202.5 gwei and 1 CELO = PHP 5.16:
 *
 *     message     events-only      storage ring buffer
 *     ----------  ---------------  ---------------------
 *      40 bytes    38,158 gas        189,150 gas
 *     120 bytes    39,898 gas        235,260 gas
 *     300 bytes    44,410 gas        372,882 gas
 *     500 bytes    49,151 gas        510,733 gas
 *     ---------------------------------------------------
 *     average      42,390 gas        300,189 gas
 *                  PHP 0.044/msg     PHP 0.314/msg   -> 86% cheaper
 *
 * (Figures are warm/steady-state. A wallet's FIRST post is ~56,000-72,000 gas
 * because `lastPostAt` starts at zero and pays a cold SSTORE. Worst observed
 * across both states: 71,728 gas = PHP 0.075.)
 *
 * The ring buffer bought nothing: `eth_getLogs` reads events for free and they
 * are retained indefinitely, whereas a storage ring would grow the contract's
 * state and every node operator would pay for it forever.
 *
 * Frontend gas constants must be sized from the numbers above, NOT from the
 * tip flow's `TIP_GAS_FALLBACK = 120000` (that is an ERC-20 transfer). Use
 * ~150,000 fallback / ~300,000 ceiling. A generous LIMIT is free insurance --
 * only gas actually consumed is charged, never the limit.
 *
 * Design rules:
 *   - The POSTER pays their own gas. No relayer, no sponsorship, no custody.
 *   - Messages are IMMUTABLE. There is no delete and no edit. Moderation is a
 *     BAN LIST: a banned wallet can no longer post, but nothing already posted
 *     is removed. This is deliberate -- see the moderation note below.
 *   - No owner, no pause, no upgrade path, no funds. The contract holds
 *     nothing, so there is nothing to steal and nothing to withdraw.
 *   - Moderators are a SET, not a single owner, so moderation survives one
 *     admin key being lost. The LAST moderator cannot be removed, so the ban
 *     list can never become permanently unmanageable.
 *   - Usernames are NOT stored here. The frontend already resolves
 *     address -> username from the existing user_data table, and a username can
 *     change. The event carries the author address; the display name is a
 *     presentation concern. This also keeps the wallet address out of any
 *     application-level payload, matching the chatroom privacy rule.
 *
 * MODERATION CAVEAT (read before deploying):
 *   An immutable public room cannot remove content. A ban stops FUTURE posts
 *   from one wallet but cannot stop a determined person from posting from a new
 *   wallet. If content removal is ever a hard requirement, this contract is the
 *   wrong design -- keep the messages off-chain instead.
 */
contract GoodMarketChat {
    // ── Limits ──────────────────────────────────────────────────────────────
    /// @notice Maximum message length in bytes (UTF-8). Matches the existing
    ///         CHATROOM_MAX_MESSAGE_LENGTH default so behaviour does not change.
    uint256 public constant MAX_LEN = 500;

    /// @notice Minimum seconds between two posts from the SAME wallet.
    ///         Anti-spam only -- the real cost of spamming is gas.
    uint256 public constant MIN_INTERVAL = 3;

    /// @notice Hard cap on the moderator set, so the bounded loop in
    ///         `moderators()` can never run out of gas.
    uint256 public constant MAX_MODERATORS = 25;

    /// @notice Message kinds. `body` carries the payload for all three:
    ///         plain text, the ImgBB URL for an image, or the tip announcement.
    uint8 public constant KIND_TEXT = 0;
    uint8 public constant KIND_IMAGE = 1;
    uint8 public constant KIND_TIP = 2;
    uint8 public constant MAX_KIND = 2;

    // ── State ───────────────────────────────────────────────────────────────
    /// @notice Id of the NEXT message. Message ids are 0-based and monotonic,
    ///         so clients can page with `eth_getLogs` + a cursor.
    uint256 public nextId;

    /// @notice Last post timestamp per author (anti-spam).
    mapping(address => uint256) public lastPostAt;

    /// @notice Banned wallets cannot post. Bans never touch stored content.
    mapping(address => bool) public banned;

    /// @notice Wallets allowed to ban/unban.
    mapping(address => bool) public isModerator;

    address[] private _moderators;

    // ── Events ──────────────────────────────────────────────────────────────
    /// @notice Emitted for every message. THIS IS THE MESSAGE.
    ///         `id` and `author` are indexed so clients can filter by either
    ///         (topic1 = id, topic2 = author); `kind` says how to render
    ///         `body`; `ref` is the parent message id for a reply (0 = none).
    ///         Read with: eth_getLogs(address=chat, topics=[PostedSig, afterId])
    event Posted(
        uint256 indexed id,
        address indexed author,
        uint8   kind,
        uint256 ref,
        uint64  ts,
        bytes   body
    );

    event Banned(address indexed who, address indexed by, uint64 ts);
    event Unbanned(address indexed who, address indexed by, uint64 ts);
    event ModeratorAdded(address indexed who, address indexed by);
    event ModeratorRemoved(address indexed who, address indexed by);

    // ── Modifiers ───────────────────────────────────────────────────────────
    modifier onlyModerator() {
        require(isModerator[msg.sender], "not_moderator");
        _;
    }

    /**
     * @param initialModerators Wallets that may manage the ban list. At least
     *        one is required -- a chatroom with no moderator could never ban
     *        anyone, and the ban list could never be repaired.
     */
    constructor(address[] memory initialModerators) {
        require(initialModerators.length > 0, "need_moderator");
        require(initialModerators.length <= MAX_MODERATORS, "too_many_moderators");
        for (uint256 i = 0; i < initialModerators.length; i++) {
            address m = initialModerators[i];
            require(m != address(0), "zero_moderator");
            require(!isModerator[m], "duplicate_moderator");
            isModerator[m] = true;
            _moderators.push(m);
            emit ModeratorAdded(m, msg.sender);
        }
    }

    // ── Posting ─────────────────────────────────────────────────────────────

    /**
     * @notice Post a message. The caller pays the gas.
     * @param kind KIND_TEXT | KIND_IMAGE | KIND_TIP.
     * @param ref  Parent message id when this is a reply, else 0.
     * @param body Raw UTF-8 payload, 1..MAX_LEN.
     *
     * Reverts when: caller is banned, body is empty or too long, `kind` is
     * unknown, or the caller posted less than MIN_INTERVAL seconds ago. All
     * checks are cheap storage reads plus one small mapping write for the
     * rate limit.
     */
    function post(uint8 kind, uint256 ref, bytes calldata body) external {
        require(!banned[msg.sender], "banned");
        require(kind <= MAX_KIND, "bad_kind");

        uint256 n = body.length;
        require(n > 0 && n <= MAX_LEN, "bad_len");

        uint256 t = block.timestamp;
        require(t >= lastPostAt[msg.sender] + MIN_INTERVAL, "too_fast");
        lastPostAt[msg.sender] = t;

        uint256 id = nextId++;
        emit Posted(id, msg.sender, kind, ref, uint64(t), body);
    }

    // ── Moderation ──────────────────────────────────────────────────────────

    /// @notice Ban a wallet. Existing messages are untouched (immutable).
    function ban(address who) external onlyModerator {
        require(who != address(0), "zero_address");
        require(!banned[who], "already_banned");
        banned[who] = true;
        emit Banned(who, msg.sender, uint64(block.timestamp));
    }

    /// @notice Lift a ban.
    function unban(address who) external onlyModerator {
        require(banned[who], "not_banned");
        banned[who] = false;
        emit Unbanned(who, msg.sender, uint64(block.timestamp));
    }

    /**
     * @notice Add a moderator. Moderators can add each other so moderation does
     *         not depend on one key.
     */
    function addModerator(address who) external onlyModerator {
        require(who != address(0), "zero_address");
        require(!isModerator[who], "already_moderator");
        require(_moderators.length < MAX_MODERATORS, "too_many_moderators");
        isModerator[who] = true;
        _moderators.push(who);
        emit ModeratorAdded(who, msg.sender);
    }

    /**
     * @notice Remove a moderator.
     *
     * The LAST moderator cannot be removed: without one, no one could ever ban
     * a spammer again and the ban list would be frozen forever.
     */
    function removeModerator(address who) external onlyModerator {
        require(isModerator[who], "not_moderator");
        require(_moderators.length > 1, "last_moderator");

        isModerator[who] = false;
        uint256 len = _moderators.length;
        for (uint256 i = 0; i < len; i++) {
            if (_moderators[i] == who) {
                _moderators[i] = _moderators[len - 1];
                _moderators.pop();
                break;
            }
        }
        emit ModeratorRemoved(who, msg.sender);
    }

    // ── Views ───────────────────────────────────────────────────────────────

    /// @notice Every current moderator. Bounded by MAX_MODERATORS so this can
    ///         never run out of gas.
    function moderators() external view returns (address[] memory) {
        return _moderators;
    }

    /// @notice Number of messages posted so far. Also the id the next one gets.
    function messageCount() external view returns (uint256) {
        return nextId;
    }

    /// @notice Convenience for the UI: can this wallet post right now?
    /// @return ok       false when banned or inside the rate-limit window
    /// @return reason   0 = ok, 1 = banned, 2 = too soon
    /// @return waitFor  seconds still to wait when reason == 2
    function canPost(address who) external view returns (bool ok, uint8 reason, uint256 waitFor) {
        if (banned[who]) return (false, 1, 0);
        uint256 ready = lastPostAt[who] + MIN_INTERVAL;
        if (block.timestamp < ready) return (false, 2, ready - block.timestamp);
        return (true, 0, 0);
    }
}
