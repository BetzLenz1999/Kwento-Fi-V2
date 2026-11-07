"""Community Withdrawal Vault runtime and EIP-712 signing helpers.

This module deliberately keeps the issuer key server-side. It does not submit
withdrawal transactions: the member wallet calls the vault directly and pays
gas. Configuration is fail-closed and the feature remains disabled unless the
operator explicitly sets COMMUNITY_WITHDRAWAL_ENABLED=YES.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

COMMUNITY_ID_TEXT = "betzlenz@goodmarketph.v2"
COMMUNITY_PROJECT_ID = "0xe4772aeb1ac8e78ecc6c0f9255e6c828ef90f0f411ba199aa090efd81271de5e"
CELO_CHAIN_ID = 42220
EIP712_NAME = "GoodMarket Community Withdrawal Vault"
EIP712_VERSION = "1"
MAX_AUTHORIZATION_LIFETIME_SECONDS = 15 * 60
KNOWN_VAULT_ADDRESS = "0x4c0d1f17e83776aa696848b870444e54ec3a2203"
KNOWN_VAULT_DEPLOYMENT_BLOCK = 81001254
MAX_RECONCILIATION_LOG_CALLS = 50
MAX_RECONCILIATION_BLOCKS = 100000

WITHDRAWAL_AUTHORIZATION_TYPES = [
    {"name": "recipient", "type": "address"},
    {"name": "amount", "type": "uint256"},
    {"name": "withdrawalId", "type": "bytes32"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
    {"name": "projectId", "type": "bytes32"},
]

VAULT_ABI = [
    {"type": "function", "name": "goodDollarToken", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "issuerSigner", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "communityProjectId", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bytes32"}]},
    {"type": "function", "name": "expectedChainId", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "minimumWithdrawalAmount", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "maximumWithdrawalAmount", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "dailyOutflowCap", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "currentUtcDayRemaining", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "paused", "stateMutability": "view", "inputs": [], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "nonces", "stateMutability": "view", "inputs": [{"name": "member", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "withdraw", "stateMutability": "nonpayable", "inputs": [
        {"name": "authorization", "type": "tuple", "components": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "withdrawalId", "type": "bytes32"},
            {"name": "nonce", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "projectId", "type": "bytes32"},
        ]},
        {"name": "issuerAuthorization", "type": "bytes"},
    ], "outputs": []},
    {"type": "event", "name": "WithdrawalPaid", "anonymous": False, "inputs": [
        {"name": "recipient", "type": "address", "indexed": True},
        {"name": "amount", "type": "uint256", "indexed": False},
        {"name": "withdrawalId", "type": "bytes32", "indexed": True},
        {"name": "nonce", "type": "uint256", "indexed": False},
        {"name": "utcDay", "type": "uint256", "indexed": False},
    ]},
]

TOKEN_ABI = [
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable", "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"type": "event", "name": "Transfer", "anonymous": False, "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "value", "type": "uint256", "indexed": False},
    ]},
]


class WithdrawalConfigError(RuntimeError):
    """Runtime settings or on-chain immutable configuration are invalid.

    Carries a stable ``code`` and a ``hint`` so a route can tell an operator
    WHAT to fix instead of collapsing every cause into one sentence. Both are
    optional, so an error raised by a caller that does not classify itself still
    works (``code`` defaults to ``"unclassified"``).

    The message itself is safe to show to an operator: it names environment
    variables and immutable-vault mismatches, and never echoes a value (the one
    address-shaped input is the caller's own configuration, already visible in
    the dashboard payload).
    """

    def __init__(self, message: str, *, code: str = "unclassified",
                 hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint

    def operator_message(self) -> str:
        """The reason plus the fix, for an admin-only surface."""
        reason = str(self)
        return f"{reason} {self.hint}" if self.hint else reason


class WithdrawalPolicyError(RuntimeError):
    """A validly configured vault cannot pay this withdrawal right now."""


@dataclass(frozen=True)
class Runtime:
    web3: object
    vault_address: str
    token_address: str
    expected_issuer_address: str
    project_id: str
    decimals: int
    issuer_private_key: bytes | None = field(repr=False)
    vault: object = field(repr=False)
    token: object = field(repr=False)
    min_amount: int
    max_amount: int
    daily_cap: int
    confirmations: int


def _env_address(env: dict, name: str, web3_cls) -> str:
    value = str(env.get(name, "")).strip()
    if not web3_cls.is_address(value):
        raise WithdrawalConfigError(
            f"{name} must be a valid EVM address.",
            code="bad_address",
            hint=f"Set {name} to a valid 0x-prefixed EVM address.",
        )
    return web3_cls.to_checksum_address(value)


def feature_enabled(env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("COMMUNITY_WITHDRAWAL_ENABLED", "")).strip().upper() == "YES"


def build_runtime(env: dict | None = None, *, web3_instance=None,
                  require_enabled: bool = True,
                  require_signer_key: bool = True) -> Runtime:
    """Validate Vercel settings against the fixed project and live vault getters."""
    env = os.environ if env is None else env
    if require_enabled and not feature_enabled(env):
        raise WithdrawalConfigError(
            "COMMUNITY_WITHDRAWAL_ENABLED is not set to YES, so new Community "
            "withdrawals are switched off.",
            code="disabled",
            hint=("Set COMMUNITY_WITHDRAWAL_ENABLED=YES and redeploy to enable "
                  "new withdrawals. Funding the vault does not enable them."),
        )

    try:
        from eth_account import Account
        from web3 import Web3
        from web3.providers.rpc import HTTPProvider
    except Exception as exc:  # pragma: no cover - installation failure path
        raise WithdrawalConfigError(
            "EVM signing dependencies are unavailable.",
            code="dependencies",
            hint="Install the pinned web3 and eth-account requirements.",
        ) from exc

    if str(env.get("COMMUNITY_CHAIN_ID", "")).strip() != str(CELO_CHAIN_ID):
        raise WithdrawalConfigError(
            f"COMMUNITY_CHAIN_ID must be {CELO_CHAIN_ID}.",
            code="chain_id",
            hint=f"Set COMMUNITY_CHAIN_ID={CELO_CHAIN_ID} and redeploy.",
        )
    configured_project = str(env.get("COMMUNITY_PROJECT_ID", "")).strip().lower()
    if configured_project != COMMUNITY_PROJECT_ID:
        raise WithdrawalConfigError(
            "COMMUNITY_PROJECT_ID does not match the approved Community.",
            code="project_id",
            hint=("Set COMMUNITY_PROJECT_ID to the approved Community ID hash "
                  f"({COMMUNITY_PROJECT_ID})."),
        )

    rpc_url = str(env.get("CELO_RPC_URL", "")).strip()
    parsed = urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise WithdrawalConfigError(
            "CELO_RPC_URL must be an HTTPS Celo RPC URL.",
            code="rpc_url",
            hint="Set CELO_RPC_URL to an HTTPS Celo endpoint (e.g. https://forno.celo.org).",
        )

    vault_address = _env_address(env, "COMMUNITY_VAULT_ADDRESS", Web3)
    token_address = _env_address(env, "GOODDOLLAR_CONTRACT_ADDRESS", Web3)
    issuer_address_text = str(env.get("COMMUNITY_ISSUER_SIGNER_ADDRESS", "")).strip()
    expected_issuer = _env_address(env, "COMMUNITY_ISSUER_SIGNER_ADDRESS", Web3) if issuer_address_text else None
    private_key_text = str(env.get("COMMUNITY_ISSUER_PRIVATE_KEY", "")).strip()
    if require_signer_key and expected_issuer is None:
        raise WithdrawalConfigError(
            "COMMUNITY_ISSUER_SIGNER_ADDRESS is required for authorization signing.",
            code="issuer_address",
            hint="Set COMMUNITY_ISSUER_SIGNER_ADDRESS to the vault's immutable issuer signer.",
        )
    if require_signer_key and not re.fullmatch(r"0x[0-9a-fA-F]{64}", private_key_text):
        raise WithdrawalConfigError(
            "COMMUNITY_ISSUER_PRIVATE_KEY must be a 32-byte 0x-prefixed server secret.",
            code="issuer_key",
            hint="Set COMMUNITY_ISSUER_PRIVATE_KEY to the issuer signer's private key.",
        )
    issuer_private_key = None
    derived_issuer = None
    if require_signer_key:
        try:
            issuer_private_key = bytes.fromhex(private_key_text[2:])
            derived_issuer = Web3.to_checksum_address(Account.from_key(issuer_private_key).address)
        except Exception as exc:
            raise WithdrawalConfigError(
                "COMMUNITY_ISSUER_PRIVATE_KEY is invalid.",
                code="issuer_key",
                hint="Set COMMUNITY_ISSUER_PRIVATE_KEY to the issuer signer's private key.",
            ) from exc
        if expected_issuer and derived_issuer != expected_issuer:
            raise WithdrawalConfigError(
                "Issuer private key does not match COMMUNITY_ISSUER_SIGNER_ADDRESS.",
                code="issuer_mismatch",
                hint=("Set COMMUNITY_ISSUER_SIGNER_ADDRESS to the address derived "
                      "from COMMUNITY_ISSUER_PRIVATE_KEY."),
            )

    decimals_text = str(env.get("GOODDOLLAR_EXPECTED_DECIMALS", "")).strip()
    if not re.fullmatch(r"\d{1,3}", decimals_text):
        raise WithdrawalConfigError(
            "GOODDOLLAR_EXPECTED_DECIMALS must be set to the independently "
            "verified token decimals.",
            code="decimals",
            hint="Set GOODDOLLAR_EXPECTED_DECIMALS=18 (the live G$ token's decimals).",
        )
    expected_decimals = int(decimals_text)
    if expected_decimals > 36:
        raise WithdrawalConfigError(
            "GOODDOLLAR_EXPECTED_DECIMALS must not exceed the supported "
            "exact-decimal range of 36.",
            code="decimals",
            hint="Set GOODDOLLAR_EXPECTED_DECIMALS=18 (the live G$ token's decimals).",
        )
    confirmations_text = str(env.get("COMMUNITY_WITHDRAWAL_CONFIRMATIONS", "5")).strip()
    if not re.fullmatch(r"\d{1,2}", confirmations_text) or not 1 <= int(confirmations_text) <= 64:
        raise WithdrawalConfigError(
            "COMMUNITY_WITHDRAWAL_CONFIRMATIONS must be an integer from 1 to 64.",
            code="confirmations",
            hint="Set COMMUNITY_WITHDRAWAL_CONFIRMATIONS to a value from 1 to 64.",
        )

    try:
        w3 = web3_instance or Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            raise WithdrawalConfigError(
                "Celo RPC is unavailable.",
                code="rpc_unreachable",
                hint="Check CELO_RPC_URL; the endpoint did not answer.",
            )
        if int(w3.eth.chain_id) != CELO_CHAIN_ID:
            raise WithdrawalConfigError(
                "CELO_RPC_URL returned the wrong chain ID.",
                code="rpc_chain",
                hint=f"Point CELO_RPC_URL at a Celo mainnet ({CELO_CHAIN_ID}) endpoint.",
            )
        if not w3.eth.get_code(vault_address) or bytes(w3.eth.get_code(vault_address)) in (b"", b"\x00"):
            raise WithdrawalConfigError(
                "COMMUNITY_VAULT_ADDRESS has no contract code.",
                code="vault_not_deployed",
                hint=("The vault contract is not deployed at COMMUNITY_VAULT_ADDRESS. "
                      "Deploy contracts/community_withdrawal_vault and set the "
                      "address the deploy script prints."),
            )
        if not w3.eth.get_code(token_address) or bytes(w3.eth.get_code(token_address)) in (b"", b"\x00"):
            raise WithdrawalConfigError(
                "GOODDOLLAR_CONTRACT_ADDRESS has no contract code.",
                code="token_not_deployed",
                hint="Set GOODDOLLAR_CONTRACT_ADDRESS to the live G$ token on Celo.",
            )
        vault = w3.eth.contract(address=vault_address, abi=VAULT_ABI)
        token = w3.eth.contract(address=token_address, abi=TOKEN_ABI)
        onchain = {
            "token": Web3.to_checksum_address(vault.functions.goodDollarToken().call()),
            "issuer": Web3.to_checksum_address(vault.functions.issuerSigner().call()),
            "project": Web3.to_hex(vault.functions.communityProjectId().call()),
            "chain": int(vault.functions.expectedChainId().call()),
            "decimals": int(token.functions.decimals().call()),
            "minimum": int(vault.functions.minimumWithdrawalAmount().call()),
            "maximum": int(vault.functions.maximumWithdrawalAmount().call()),
            "daily_cap": int(vault.functions.dailyOutflowCap().call()),
        }
        if onchain["token"] != token_address:
            raise WithdrawalConfigError(
                "Vault token does not match the approved runtime settings.",
                code="vault_token_mismatch",
                hint="Set GOODDOLLAR_CONTRACT_ADDRESS to the token the vault was deployed with.",
            )
        if expected_issuer and onchain["issuer"] != expected_issuer:
            raise WithdrawalConfigError(
                "Vault issuer does not match the approved runtime settings.",
                code="vault_issuer_mismatch",
                hint="Set COMMUNITY_ISSUER_SIGNER_ADDRESS to the vault's immutable issuerSigner.",
            )
        if derived_issuer and onchain["issuer"] != derived_issuer:
            raise WithdrawalConfigError(
                "Issuer private key does not match the vault's immutable issuer signer.",
                code="vault_issuer_mismatch",
                hint=("Use the private key for the vault's immutable issuerSigner, "
                      "or migrate to a vault deployed with this key."),
            )
        if onchain["project"].lower() != COMMUNITY_PROJECT_ID or onchain["chain"] != CELO_CHAIN_ID:
            raise WithdrawalConfigError(
                "Vault immutable Community project or chain does not match deployment policy.",
                code="vault_project_mismatch",
                hint="This vault was deployed for a different Community ID or chain.",
            )
        if onchain["decimals"] != expected_decimals:
            raise WithdrawalConfigError(
                "Configured GoodDollar decimals do not match the live token.",
                code="decimals_mismatch",
                hint=f"Set GOODDOLLAR_EXPECTED_DECIMALS={onchain['decimals']} "
                     "(the live token's decimals).",
            )
        if onchain["minimum"] <= 0 or onchain["maximum"] < onchain["minimum"] or onchain["daily_cap"] < onchain["maximum"]:
            raise WithdrawalConfigError(
                "Vault immutable withdrawal limits are invalid.",
                code="vault_limits",
                hint=("The deployed vault's limits are inconsistent. Limits are "
                      "immutable, so this needs a redeploy with valid limits."),
            )
        return Runtime(
            web3=w3,
            vault_address=vault_address,
            token_address=token_address,
            expected_issuer_address=onchain["issuer"],
            project_id=COMMUNITY_PROJECT_ID,
            decimals=expected_decimals,
            issuer_private_key=issuer_private_key,
            vault=vault,
            token=token,
            min_amount=onchain["minimum"],
            max_amount=onchain["maximum"],
            daily_cap=onchain["daily_cap"],
            confirmations=int(confirmations_text),
        )
    except WithdrawalConfigError:
        raise
    except Exception as exc:
        raise WithdrawalConfigError(
            "Could not verify Community vault configuration on Celo.",
            code="verification_failed",
            hint="Check the Celo RPC and the vault getters; the verification call failed.",
        ) from exc


#: Member-facing copy for a configuration failure. Deliberately generic: a
#: member cannot act on an operator's configuration, and naming an env var or a
#: contract getter in a member-facing string leaks deployment detail without
#: helping them. The operator sees the real reason on the admin surface.
MEMBER_CONFIG_ERROR = (
    "Community withdrawals are temporarily unavailable. Please try again later."
)


def operator_detail(exc: BaseException) -> str:
    """The fixable reason behind a :class:`WithdrawalConfigError`.

    Used by admin-only surfaces (and by the log line for member-facing ones) so
    an operator is told WHAT to change instead of a generic sentence. Falls back
    to the exception's own text for any other error type.
    """
    if isinstance(exc, WithdrawalConfigError):
        return exc.operator_message()
    return str(exc)


def deployment_block_for_runtime(runtime: Runtime) -> int:
    """Return the configured history/reconciliation start block.

    The known-vault fallback is retained for compatibility with the existing
    history endpoint.  A different vault must always have an explicit start
    block so reconciliation cannot scan an accidental or unbounded range.
    """
    configured = os.environ.get("COMMUNITY_VAULT_DEPLOYMENT_BLOCK", "").strip()
    if configured:
        if not re.fullmatch(r"\d+", configured):
            raise WithdrawalConfigError(
                "COMMUNITY_VAULT_DEPLOYMENT_BLOCK must be a non-negative integer.",
                code="deployment_block",
                hint="Set COMMUNITY_VAULT_DEPLOYMENT_BLOCK to the vault deployment block.",
            )
        return int(configured)
    if str(runtime.vault_address).lower() == KNOWN_VAULT_ADDRESS:
        return KNOWN_VAULT_DEPLOYMENT_BLOCK
    raise WithdrawalConfigError(
        "Withdrawal history is not configured for this vault.",
        code="deployment_block",
        hint="Set COMMUNITY_VAULT_DEPLOYMENT_BLOCK to the vault deployment block.",
    )


def amount_to_base_units(amount_gd: Decimal | str, decimals: int) -> int:
    """Convert without rounding; ledger value must be exactly token-representable."""
    try:
        amount = amount_gd if isinstance(amount_gd, Decimal) else Decimal(str(amount_gd))
        scaled = amount * (Decimal(10) ** int(decimals))
        integral = scaled.to_integral_value()
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WithdrawalPolicyError("Ledger amount is not a valid decimal G$ value") from exc
    if not amount.is_finite() or amount <= 0 or scaled != integral:
        raise WithdrawalPolicyError("Ledger amount cannot be represented exactly in GoodDollar token units")
    return int(integral)


def base_units_to_gd(amount: int, decimals: int) -> Decimal:
    return Decimal(int(amount)) / (Decimal(10) ** int(decimals))


def sign_authorization(private_key: bytes | str, *, domain: dict, message: dict) -> str:
    """Sign the exact Solidity WithdrawalAuthorization EIP-712 schema."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        full_message = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "WithdrawalAuthorization": WITHDRAWAL_AUTHORIZATION_TYPES,
            },
            "primaryType": "WithdrawalAuthorization",
            "domain": {
                "name": EIP712_NAME,
                "version": EIP712_VERSION,
                "chainId": CELO_CHAIN_ID,
                "verifyingContract": domain["verifyingContract"],
            },
            "message": {
                "recipient": message["recipient"],
                "amount": int(message["amount"]),
                "withdrawalId": message["withdrawalId"],
                "nonce": int(message["nonce"]),
                "deadline": int(message["deadline"]),
                "projectId": COMMUNITY_PROJECT_ID,
            },
        }
        signature = Account.sign_message(encode_typed_data(full_message=full_message), private_key=private_key).signature
        return "0x" + bytes(signature).hex()
    except Exception as exc:
        raise WithdrawalConfigError(
            "Could not create the Community EIP-712 authorization.",
            code="signing_failed",
            hint=("Check COMMUNITY_ISSUER_PRIVATE_KEY and the EIP-712 domain; "
                  "signing the authorization failed."),
        ) from exc


def public_config(runtime: Runtime) -> dict:
    """Safe public deployment details for the authorized Admin Dashboard only."""
    return {
        "success": True,
        "runtime_verified": True,
        "vault_address": runtime.vault_address,
        "token_address": runtime.token_address,
        "issuer_signer_address": runtime.expected_issuer_address,
        "chain_id": CELO_CHAIN_ID,
        "project_id": COMMUNITY_PROJECT_ID,
        "decimals": runtime.decimals,
        "minimum_base_units": str(runtime.min_amount),
        "maximum_base_units": str(runtime.max_amount),
        "daily_cap_base_units": str(runtime.daily_cap),
    }


def decode_withdrawal_transaction(runtime: Runtime, tx: dict, row: dict) -> None:
    """Require a submitted transaction to be the exact stored member call."""
    from web3 import Web3
    sender = Web3.to_checksum_address(tx.get("from", ""))
    recipient = Web3.to_checksum_address(row["wallet_address"])
    tx_to = Web3.to_checksum_address(tx.get("to") or "")
    vault_address = Web3.to_checksum_address(runtime.vault_address)
    if sender != recipient or tx_to != vault_address or int(tx.get("value", 0)) != 0:
        raise WithdrawalPolicyError("Transaction sender, destination, or native value does not match the authorization")
    fn, args = runtime.vault.decode_function_input(tx.get("input") or tx.get("data"))
    if fn.fn_name != "withdraw":
        raise WithdrawalPolicyError("Transaction is not a direct Community vault withdrawal")
    signed = args["authorization"]
    # SQL persists the authorization as scalar columns (not a JSON object).
    # Reconstruct from that immutable reservation so recovery/confirmation
    # validates the exact authorization that was signed.
    expected = {
        "recipient": row["wallet_address"],
        "amount": row["amount_base_units"],
        "withdrawalId": row["withdrawal_id"],
        "nonce": row["authorization_nonce"],
        "deadline": row["authorization_deadline"],
        "projectId": row["project_id"],
    }
    expected_tuple = (
        Web3.to_checksum_address(expected["recipient"]), int(expected["amount"]),
        expected["withdrawalId"], int(expected["nonce"]), int(expected["deadline"]), expected["projectId"],
    )
    tuple_fields = ("recipient", "amount", "withdrawalId", "nonce", "deadline", "projectId")
    try:
        actual_tuple = (tuple(signed[field_name] for field_name in tuple_fields)
                        if isinstance(signed, Mapping) else tuple(signed))
    except (KeyError, TypeError) as exc:
        raise WithdrawalPolicyError("Transaction authorization tuple is malformed") from exc
    if len(actual_tuple) != len(tuple_fields):
        raise WithdrawalPolicyError("Transaction authorization tuple is malformed")
    actual_id = Web3.to_hex(actual_tuple[2]) if not isinstance(actual_tuple[2], str) else actual_tuple[2]
    actual_project = Web3.to_hex(actual_tuple[5]) if not isinstance(actual_tuple[5], str) else actual_tuple[5]
    normalized_actual = (Web3.to_checksum_address(actual_tuple[0]), int(actual_tuple[1]), actual_id, int(actual_tuple[3]), int(actual_tuple[4]), actual_project)
    normalized_expected = (expected_tuple[0], expected_tuple[1], expected_tuple[2].lower(), expected_tuple[3], expected_tuple[4], expected_tuple[5].lower())
    normalized_actual = (normalized_actual[0], normalized_actual[1], str(normalized_actual[2]).lower(), normalized_actual[3], normalized_actual[4], str(normalized_actual[5]).lower())
    if normalized_actual != normalized_expected:
        raise WithdrawalPolicyError("Transaction authorization does not match the stored Community request")
    supplied_signature = args["issuerAuthorization"]
    supplied_hex = "0x" + bytes(supplied_signature).hex() if not isinstance(supplied_signature, str) else supplied_signature
    if supplied_hex.lower() != str(row["authorization_signature"]).lower():
        raise WithdrawalPolicyError("Transaction signature does not match the stored Community authorization")


def verify_withdrawal_receipt(runtime: Runtime, receipt: dict, row: dict) -> bool:
    """Require one exact WithdrawalPaid event from this vault receipt."""
    from web3 import Web3
    from web3.logs import DISCARD
    try:
        logs = runtime.vault.events.WithdrawalPaid().process_receipt(receipt, errors=DISCARD)
    except Exception:
        return False
    expected_id = str(row["withdrawal_id"]).lower()
    expected_wallet = str(row["wallet_address"]).lower()
    expected_amount = int(row["amount_base_units"])
    expected_nonce = int(row["authorization_nonce"])
    for log in logs:
        if (Web3.to_checksum_address(log["address"])
                != Web3.to_checksum_address(runtime.vault_address)):
            continue
        args = log["args"]
        got_id = Web3.to_hex(args["withdrawalId"])
        if (str(args["recipient"]).lower() == expected_wallet
                and got_id.lower() == expected_id
                and int(args["amount"]) == expected_amount
                and int(args["nonce"]) == expected_nonce):
            return True
    return False


def find_withdrawal_transaction(runtime: Runtime, row: dict) -> str | None:
    """Recover a recent exact payout event for a reservation with a lost tx hash."""
    from web3 import Web3
    latest = int(runtime.web3.eth.block_number)
    topic0 = Web3.keccak(text="WithdrawalPaid(address,uint256,bytes32,uint256,uint256)")
    withdrawal_id = bytes.fromhex(str(row["withdrawal_id"])[2:])
    logs = runtime.web3.eth.get_logs({
        "address": runtime.vault_address,
        "fromBlock": max(0, latest - 10000),
        "toBlock": latest,
        "topics": [topic0, None, withdrawal_id],
    })
    for log in logs:
        tx_hash = Web3.to_hex(log["transactionHash"]).lower()
        try:
            receipt = runtime.web3.eth.get_transaction_receipt(tx_hash)
            tx = runtime.web3.eth.get_transaction(tx_hash)
            if int(receipt.get("status", 0)) != 1:
                continue
            decode_withdrawal_transaction(runtime, tx, row)
            if verify_withdrawal_receipt(runtime, receipt, row):
                return tx_hash
        except Exception:
            continue
    return None


def scan_withdrawal_transaction_page(
    runtime: Runtime,
    row: dict,
    *,
    deployment_block: int,
    before_block: int | None = None,
) -> dict:
    """Scan one bounded descending page for the exact stored payout.

    This helper is deliberately read-only.  It returns a deterministic status
    result and never records a hash, marks a reservation failed, or finalizes
    a ledger row.  A candidate is returned only after transaction calldata,
    stored issuer signature, successful receipt, and exact payout event all
    validate through the same primitives used by confirmation.
    """
    from web3 import Web3

    try:
        deployment_block = int(deployment_block)
        if deployment_block < 0:
            return {"status": "manual_review"}
        end_block = int(runtime.web3.eth.block_number)
        if before_block is not None:
            if isinstance(before_block, bool):
                return {"status": "invalid_cursor"}
            try:
                cursor = int(before_block)
            except (TypeError, ValueError):
                return {"status": "invalid_cursor"}
            if cursor < deployment_block or cursor > end_block:
                return {"status": "invalid_cursor"}
            end_block = cursor
        if end_block < deployment_block:
            return {"status": "manual_review"}

        wallet = Web3.to_checksum_address(str(row.get("wallet_address") or ""))
        withdrawal_id = str(row.get("withdrawal_id") or "").strip().lower()
        signature = str(row.get("authorization_signature") or "").strip()
        if (str(row.get("status") or "") != "claimed" or not signature
                or not re.fullmatch(r"0x[0-9a-f]{64}", withdrawal_id)):
            return {"status": "manual_review"}
        row_vault = str(row.get("vault_address") or "").strip()
        row_token = str(row.get("token_address") or "").strip()
        if (not Web3.is_address(row_vault)
                or Web3.to_checksum_address(row_vault)
                != Web3.to_checksum_address(runtime.vault_address)):
            return {"status": "manual_review"}
        if (not Web3.is_address(row_token)
                or Web3.to_checksum_address(row_token)
                != Web3.to_checksum_address(runtime.token_address)):
            return {"status": "manual_review"}
        if row.get("chain_id") is None or int(row["chain_id"]) != CELO_CHAIN_ID:
            return {"status": "manual_review"}
        if str(row.get("project_id") or "").lower() != str(runtime.project_id).lower():
            return {"status": "manual_review"}
        withdrawal_id_bytes = Web3.to_bytes(hexstr=withdrawal_id)
        if len(withdrawal_id_bytes) != 32:
            return {"status": "manual_review"}

        try:
            max_chunk = int(os.environ.get("GETLOGS_MAX_CHUNK", "4000"))
        except (TypeError, ValueError):
            return {"status": "rpc_error"}
        chunk = max(500, min(max_chunk, 4000))
        max_scan_blocks = min(MAX_RECONCILIATION_BLOCKS,
                              chunk * MAX_RECONCILIATION_LOG_CALLS)
        topic0 = Web3.keccak(text="WithdrawalPaid(address,uint256,bytes32,uint256,uint256)")
        recipient_topic = "0x" + wallet.lower().removeprefix("0x").rjust(64, "0")
        page_to = end_block
        page_from = None
        scanned_blocks = 0
        calls = 0
        seen_hashes = set()
        incomplete_rpc = False

        while (end_block >= deployment_block
               and calls < MAX_RECONCILIATION_LOG_CALLS
               and scanned_blocks < max_scan_blocks):
            eligible_span = min(chunk, end_block - deployment_block + 1,
                                max_scan_blocks - scanned_blocks)
            start_block = max(deployment_block, end_block - eligible_span + 1)
            page_from = (max(deployment_block, start_block)
                         if page_from is None else min(page_from, max(deployment_block, start_block)))
            try:
                logs = runtime.web3.eth.get_logs({
                    "address": runtime.vault_address,
                    "fromBlock": start_block,
                    "toBlock": end_block,
                    "topics": [topic0, recipient_topic, withdrawal_id_bytes],
                })
            except Exception:
                return {"status": "rpc_error"}
            calls += 1

            logs = sorted(
                logs,
                key=lambda item: (int(item["blockNumber"]), int(item["logIndex"])),
                reverse=True,
            )
            for log in logs:
                try:
                    tx_hash = Web3.to_hex(log["transactionHash"]).lower()
                    if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash) or tx_hash in seen_hashes:
                        continue
                    if int(log.get("blockNumber", 0)) < deployment_block:
                        continue
                    seen_hashes.add(tx_hash)
                    if Web3.to_checksum_address(log.get("address") or "") != Web3.to_checksum_address(runtime.vault_address):
                        continue
                    raw_topics = log.get("topics") or []
                    if len(raw_topics) != 3:
                        continue
                    topics = [Web3.to_bytes(hexstr=item) if isinstance(item, str) else bytes(item)
                              for item in raw_topics]
                    if (len(topics[0]) != 32 or len(topics[1]) != 32
                            or len(topics[2]) != 32 or topics[0] != topic0
                            or topics[1][-20:] != bytes.fromhex(wallet[2:].lower())
                            or topics[2] != withdrawal_id_bytes):
                        continue
                    try:
                        tx = runtime.web3.eth.get_transaction(tx_hash)
                    except Exception:
                        incomplete_rpc = True
                        continue
                    try:
                        receipt = runtime.web3.eth.get_transaction_receipt(tx_hash)
                    except Exception:
                        incomplete_rpc = True
                        continue
                    if int(receipt.get("status", 0)) != 1:
                        continue
                    if "chainId" in tx and int(tx["chainId"]) != CELO_CHAIN_ID:
                        continue
                    decode_withdrawal_transaction(runtime, tx, row)
                    if verify_withdrawal_receipt(runtime, receipt, row):
                        return {
                            "status": "transaction_found",
                            "tx_hash": tx_hash,
                        }
                except WithdrawalPolicyError:
                    continue
                except Exception:
                    # A malformed candidate is simply not an exact match.
                    continue

            scanned_blocks += eligible_span
            end_block -= eligible_span

        if incomplete_rpc:
            return {"status": "rpc_error"}
        if end_block < deployment_block:
            return {"status": "not_found"}
        return {
            "status": "scanning",
            "next_before_block": end_block,
            "scanned_blocks": scanned_blocks,
            "from_block": page_from,
            "to_block": page_to,
        }
    except Exception:
        return {"status": "rpc_error"}


def verify_funding_receipt(runtime: Runtime, receipt: dict, tx: dict, admin_wallet: str) -> int:
    """Validate an admin-wallet ERC-20 transfer directly into the configured vault."""
    from web3 import Web3
    from web3.logs import DISCARD
    if int(tx.get("chainId", CELO_CHAIN_ID)) != CELO_CHAIN_ID:
        raise WithdrawalPolicyError("Funding transaction must be on Celo Mainnet")
    if Web3.to_checksum_address(tx.get("from", "")) != Web3.to_checksum_address(admin_wallet):
        raise WithdrawalPolicyError("Funding transaction sender does not match the connected admin wallet")
    if Web3.to_checksum_address(tx.get("to") or "") != runtime.token_address:
        raise WithdrawalPolicyError("Funding transaction must call the approved GoodDollar token")
    if int(receipt.get("status", 0)) != 1:
        raise WithdrawalPolicyError("Funding transaction did not succeed")
    fn, args = runtime.token.decode_function_input(tx.get("input") or tx.get("data"))
    if fn.fn_name != "transfer" or Web3.to_checksum_address(args["to"]) != runtime.vault_address:
        raise WithdrawalPolicyError("Funding transaction must transfer GoodDollar directly to the Community vault")
    expected_amount = int(args["value"])
    matching = []
    try:
        events = runtime.token.events.Transfer().process_receipt(receipt, errors=DISCARD)
    except Exception as exc:
        raise WithdrawalPolicyError("Could not decode the GoodDollar Transfer event") from exc
    for event in events:
        ev = event["args"]
        if (Web3.to_checksum_address(ev["from"]) == Web3.to_checksum_address(admin_wallet)
                and Web3.to_checksum_address(ev["to"]) == runtime.vault_address
                and int(ev["value"]) == expected_amount):
            matching.append(event)
    if not matching or expected_amount <= 0:
        raise WithdrawalPolicyError("No exact GoodDollar transfer to the vault was found in the receipt")
    return expected_amount
