"""Community Withdrawal Vault runtime and EIP-712 signing helpers.

This module deliberately keeps the issuer key server-side. It does not submit
withdrawal transactions: the member wallet calls the vault directly and pays
gas. Configuration is fail-closed and the feature remains disabled unless the
operator explicitly sets COMMUNITY_WITHDRAWAL_ENABLED=YES.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

COMMUNITY_ID_TEXT = "betzlenz@goodmarketph.v2"
COMMUNITY_PROJECT_ID = "0xe4772aeb1ac8e78ecc6c0f9255e6c828ef90f0f411ba199aa090efd81271de5e"
CELO_CHAIN_ID = 42220
EIP712_NAME = "GoodMarket Community Withdrawal Vault"
EIP712_VERSION = "1"
MAX_AUTHORIZATION_LIFETIME_SECONDS = 15 * 60

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
    """Runtime settings or on-chain immutable configuration are invalid."""


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
        raise WithdrawalConfigError(f"{name} must be a valid EVM address")
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
        raise WithdrawalConfigError("Community vault withdrawals are disabled")

    try:
        from eth_account import Account
        from web3 import Web3
        from web3.providers.rpc import HTTPProvider
    except Exception as exc:  # pragma: no cover - installation failure path
        raise WithdrawalConfigError("EVM signing dependencies are unavailable") from exc

    if str(env.get("COMMUNITY_CHAIN_ID", "")).strip() != str(CELO_CHAIN_ID):
        raise WithdrawalConfigError(f"COMMUNITY_CHAIN_ID must be {CELO_CHAIN_ID}")
    configured_project = str(env.get("COMMUNITY_PROJECT_ID", "")).strip().lower()
    if configured_project != COMMUNITY_PROJECT_ID:
        raise WithdrawalConfigError("COMMUNITY_PROJECT_ID does not match the approved Community")

    rpc_url = str(env.get("CELO_RPC_URL", "")).strip()
    parsed = urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise WithdrawalConfigError("CELO_RPC_URL must be an HTTPS Celo RPC URL")

    vault_address = _env_address(env, "COMMUNITY_VAULT_ADDRESS", Web3)
    token_address = _env_address(env, "GOODDOLLAR_TOKEN_ADDRESS", Web3)
    issuer_address_text = str(env.get("COMMUNITY_ISSUER_SIGNER_ADDRESS", "")).strip()
    expected_issuer = _env_address(env, "COMMUNITY_ISSUER_SIGNER_ADDRESS", Web3) if issuer_address_text else None
    private_key_text = str(env.get("COMMUNITY_ISSUER_PRIVATE_KEY", "")).strip()
    if require_signer_key and expected_issuer is None:
        raise WithdrawalConfigError("COMMUNITY_ISSUER_SIGNER_ADDRESS is required for authorization signing")
    if require_signer_key and not re.fullmatch(r"0x[0-9a-fA-F]{64}", private_key_text):
        raise WithdrawalConfigError("COMMUNITY_ISSUER_PRIVATE_KEY must be a 32-byte 0x-prefixed server secret")
    issuer_private_key = None
    derived_issuer = None
    if require_signer_key:
        try:
            issuer_private_key = bytes.fromhex(private_key_text[2:])
            derived_issuer = Web3.to_checksum_address(Account.from_key(issuer_private_key).address)
        except Exception as exc:
            raise WithdrawalConfigError("COMMUNITY_ISSUER_PRIVATE_KEY is invalid") from exc
        if expected_issuer and derived_issuer != expected_issuer:
            raise WithdrawalConfigError("Issuer private key does not match COMMUNITY_ISSUER_SIGNER_ADDRESS")

    decimals_text = str(env.get("GOODDOLLAR_EXPECTED_DECIMALS", "")).strip()
    if not re.fullmatch(r"\d{1,3}", decimals_text):
        raise WithdrawalConfigError("GOODDOLLAR_EXPECTED_DECIMALS must be set to the independently verified token decimals")
    expected_decimals = int(decimals_text)
    if expected_decimals > 36:
        raise WithdrawalConfigError("GOODDOLLAR_EXPECTED_DECIMALS must not exceed the supported exact-decimal range of 36")
    confirmations_text = str(env.get("COMMUNITY_WITHDRAWAL_CONFIRMATIONS", "5")).strip()
    if not re.fullmatch(r"\d{1,2}", confirmations_text) or not 1 <= int(confirmations_text) <= 64:
        raise WithdrawalConfigError("COMMUNITY_WITHDRAWAL_CONFIRMATIONS must be an integer from 1 to 64")

    try:
        w3 = web3_instance or Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            raise WithdrawalConfigError("Celo RPC is unavailable")
        if int(w3.eth.chain_id) != CELO_CHAIN_ID:
            raise WithdrawalConfigError("CELO_RPC_URL returned the wrong chain ID")
        if not w3.eth.get_code(vault_address) or bytes(w3.eth.get_code(vault_address)) in (b"", b"\x00"):
            raise WithdrawalConfigError("COMMUNITY_VAULT_ADDRESS has no contract code")
        if not w3.eth.get_code(token_address) or bytes(w3.eth.get_code(token_address)) in (b"", b"\x00"):
            raise WithdrawalConfigError("GOODDOLLAR_TOKEN_ADDRESS has no contract code")
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
            raise WithdrawalConfigError("Vault token does not match the approved runtime settings")
        if expected_issuer and onchain["issuer"] != expected_issuer:
            raise WithdrawalConfigError("Vault issuer does not match the approved runtime settings")
        if derived_issuer and onchain["issuer"] != derived_issuer:
            raise WithdrawalConfigError("Issuer private key does not match the vault's immutable issuer signer")
        if onchain["project"].lower() != COMMUNITY_PROJECT_ID or onchain["chain"] != CELO_CHAIN_ID:
            raise WithdrawalConfigError("Vault immutable Community project or chain does not match deployment policy")
        if onchain["decimals"] != expected_decimals:
            raise WithdrawalConfigError("Configured GoodDollar decimals do not match the live token")
        if onchain["minimum"] <= 0 or onchain["maximum"] < onchain["minimum"] or onchain["daily_cap"] < onchain["maximum"]:
            raise WithdrawalConfigError("Vault immutable withdrawal limits are invalid")
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
        raise WithdrawalConfigError("Could not verify Community vault configuration on Celo") from exc


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
        raise WithdrawalConfigError("Could not create the Community EIP-712 authorization") from exc


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
    if sender != recipient or tx_to != runtime.vault_address or int(tx.get("value", 0)) != 0:
        raise WithdrawalPolicyError("Transaction sender, destination, or native value does not match the authorization")
    fn, args = runtime.vault.decode_function_input(tx.get("input") or tx.get("data"))
    if fn.fn_name != "withdraw":
        raise WithdrawalPolicyError("Transaction is not a direct Community vault withdrawal")
    signed = args["authorization"]
    expected = row["authorization"]
    expected_tuple = (
        Web3.to_checksum_address(expected["recipient"]), int(expected["amount"]),
        expected["withdrawalId"], int(expected["nonce"]), int(expected["deadline"]), expected["projectId"],
    )
    actual_tuple = tuple(signed)
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
    try:
        logs = runtime.vault.events.WithdrawalPaid().process_receipt(receipt, errors=0)
    except Exception:
        return False
    expected_id = str(row["withdrawal_id"]).lower()
    expected_wallet = str(row["wallet_address"]).lower()
    expected_amount = int(row["amount_base_units"])
    expected_nonce = int(row["authorization_nonce"])
    for log in logs:
        if Web3.to_checksum_address(log["address"]) != runtime.vault_address:
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


def verify_funding_receipt(runtime: Runtime, receipt: dict, tx: dict, admin_wallet: str) -> int:
    """Validate an admin-wallet ERC-20 transfer directly into the configured vault."""
    from web3 import Web3
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
        events = runtime.token.events.Transfer().process_receipt(receipt, errors=0)
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
