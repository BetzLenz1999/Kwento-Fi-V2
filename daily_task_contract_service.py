"""Celo Daily Task v2 contract access and owner-bound payment authorization."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from eth_account import Account
from web3 import Web3

from config import GOODDOLLAR_CONTRACT_ADDRESS
from chain_rpc import make_provider

CHAIN_ID = 42220
SUBMISSION_FEE_WEI = 20 * 10**18
MIN_REWARD_WEI = 10 * 10**18
MAX_REWARD_WEI = 10_000 * 10**18
VOUCHER_TTL_SECONDS = 10 * 60

TOKEN_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]
CONTRACT_ABI = [
    {"inputs": [], "name": "owner", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "goodDollarToken", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "SUBMISSION_FEE", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "MIN_REWARD", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "MAX_REWARD", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "intentId", "type": "bytes32"}, {"name": "platformHash", "type": "bytes32"}, {"name": "postCommitment", "type": "bytes32"}, {"name": "rewardAmount", "type": "uint256"}, {"name": "deadline", "type": "uint256"}, {"name": "ownerSignature", "type": "bytes"}], "name": "payTaskSubmission", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "recipient", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "submissionId", "type": "bytes32"}, {"name": "platformHash", "type": "bytes32"}], "name": "disburseReward", "outputs": [{"name": "taskHash", "type": "bytes32"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "taskHash", "type": "bytes32"}], "name": "processedTasks", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "wallet", "type": "address"}, {"indexed": True, "name": "intentId", "type": "bytes32"}, {"indexed": True, "name": "platformHash", "type": "bytes32"}, {"indexed": False, "name": "postCommitment", "type": "bytes32"}, {"indexed": False, "name": "fee", "type": "uint256"}, {"indexed": False, "name": "rewardAmount", "type": "uint256"}, {"indexed": False, "name": "timestamp", "type": "uint256"}], "name": "SubmissionFeePaid", "type": "event"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "recipient", "type": "address"}, {"indexed": True, "name": "taskHash", "type": "bytes32"}, {"indexed": True, "name": "platformHash", "type": "bytes32"}, {"indexed": False, "name": "amount", "type": "uint256"}, {"indexed": False, "name": "timestamp", "type": "uint256"}], "name": "RewardDisbursed", "type": "event"},
]
PLATFORM_HASHES = {
    "twitter": Web3.keccak(text="twitter"),
    "telegram": Web3.keccak(text="telegram"),
}


class DailyTaskContractError(RuntimeError):
    """Raised when Daily Task v2 is absent, misconfigured, or fails verification."""


@dataclass
class DailyTaskContractContext:
    w3: Web3
    contract: Any
    token: Any
    account: Any
    contract_address: str
    token_address: str
    expected_signer: str


def _private_key() -> str:
    value = (os.getenv("DAILYTASK_KEY") or "").strip()
    if not value:
        raise DailyTaskContractError("DAILYTASK_KEY is not configured")
    return value if value.startswith("0x") else "0x" + value


def get_contract_context(*, require_rpc: bool = True) -> DailyTaskContractContext:
    """Return a verified Celo context; never trust an address/key pair by itself."""
    contract_address = (os.getenv("DAILY_TASK_V2_CONTRACT_ADDRESS") or "").strip()
    expected_signer = (os.getenv("DAILYTASK_SIGNER_ADDRESS") or "").strip()
    if not contract_address:
        raise DailyTaskContractError("DAILY_TASK_V2_CONTRACT_ADDRESS is not configured")
    if not expected_signer:
        raise DailyTaskContractError("DAILYTASK_SIGNER_ADDRESS is not configured")
    if not GOODDOLLAR_CONTRACT_ADDRESS:
        raise DailyTaskContractError("GOODDOLLAR_CONTRACT_ADDRESS is not configured")
    try:
        contract_address = Web3.to_checksum_address(contract_address)
        expected_signer = Web3.to_checksum_address(expected_signer)
        token_address = Web3.to_checksum_address(GOODDOLLAR_CONTRACT_ADDRESS)
        account = Account.from_key(_private_key())
    except Exception as exc:
        raise DailyTaskContractError("Daily Task contract configuration contains an invalid address or signer key") from exc
    if account.address.lower() != expected_signer.lower():
        raise DailyTaskContractError("DAILYTASK_KEY does not match DAILYTASK_SIGNER_ADDRESS")

    rpc_url = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
    w3 = Web3(make_provider(rpc_url))
    if require_rpc:
        try:
            if not w3.is_connected():
                raise DailyTaskContractError("Celo RPC connection failed")
            if int(w3.eth.chain_id) != CHAIN_ID:
                raise DailyTaskContractError("Daily Task v2 is available only on Celo Mainnet (chain 42220)")
            if not w3.eth.get_code(contract_address):
                raise DailyTaskContractError("Daily Task v2 contract has no code at the configured address")
            contract = w3.eth.contract(address=contract_address, abi=CONTRACT_ABI)
            owner = Web3.to_checksum_address(contract.functions.owner().call())
            deployed_token = Web3.to_checksum_address(contract.functions.goodDollarToken().call())
            if owner.lower() != expected_signer.lower():
                raise DailyTaskContractError("Configured signer is not the owner of the Daily Task v2 contract")
            if deployed_token.lower() != token_address.lower():
                raise DailyTaskContractError("Configured G$ token does not match the token fixed in the Daily Task v2 contract")
            if int(contract.functions.SUBMISSION_FEE().call()) != SUBMISSION_FEE_WEI:
                raise DailyTaskContractError("Daily Task v2 submission fee is not the expected 20 G$")
        except DailyTaskContractError:
            raise
        except Exception as exc:
            raise DailyTaskContractError("Unable to verify the Daily Task v2 contract on Celo") from exc
    else:
        contract = w3.eth.contract(address=contract_address, abi=CONTRACT_ABI)
    token = w3.eth.contract(address=token_address, abi=TOKEN_ABI)
    return DailyTaskContractContext(w3, contract, token, account, contract_address, token_address, expected_signer)


def reward_to_wei(value: Any) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < Decimal("10") or amount > Decimal("10000"):
            raise ValueError
        scaled = amount * Decimal(10**18)
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DailyTaskContractError("Daily Task reward must be 10–10,000 G$ with at most 18 decimal places") from exc


def commitment_for(wallet: str, platform: str, post_url: str, salt_hex: str, intent_id: bytes) -> bytes:
    if platform not in PLATFORM_HASHES:
        raise DailyTaskContractError("Unsupported Daily Task platform")
    try:
        from eth_abi import encode as abi_encode
        salt = Web3.to_bytes(hexstr=salt_hex)
        if len(salt) != 32 or len(intent_id) != 32:
            raise ValueError("invalid byte length")
        encoded = abi_encode(
            ["address", "bytes32", "bytes32", "bytes32", "bytes32"],
            [Web3.to_checksum_address(wallet), intent_id, PLATFORM_HASHES[platform], Web3.keccak(text=post_url), salt],
        )
        commitment = Web3.keccak(encoded)
        if commitment == bytes(32):
            raise ValueError("zero commitment")
        return commitment
    except Exception as exc:
        raise DailyTaskContractError("Invalid Daily Task post commitment data") from exc


def issue_payment_authorization(
    wallet: str,
    platform: str,
    commitment: bytes,
    reward_amount_wei: int,
    *,
    context: DailyTaskContractContext | None = None,
    now: int | None = None,
    intent_id: bytes | None = None,
) -> dict[str, Any]:
    if platform not in PLATFORM_HASHES:
        raise DailyTaskContractError("Unsupported Daily Task platform")
    context = context or get_contract_context()
    if reward_amount_wei < MIN_REWARD_WEI or reward_amount_wei > MAX_REWARD_WEI:
        raise DailyTaskContractError("Configured reward is outside the contract's 10–10,000 G$ limits")
    intent_id = intent_id or secrets.token_bytes(32)
    if len(intent_id) != 32:
        raise DailyTaskContractError("Payment intent must be exactly 32 bytes")
    import time
    issued_at = int(now if now is not None else time.time())
    deadline = issued_at + VOUCHER_TTL_SECONDS
    domain = {
        "name": "KwentoFi Daily Task Rewards",
        "version": "2",
        "chainId": CHAIN_ID,
        "verifyingContract": context.contract_address,
    }
    message_types = {
        "TaskPayment": [
            {"name": "wallet", "type": "address"},
            {"name": "intentId", "type": "bytes32"},
            {"name": "platformHash", "type": "bytes32"},
            {"name": "postCommitment", "type": "bytes32"},
            {"name": "rewardAmount", "type": "uint256"},
            {"name": "fee", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
        ]
    }
    message = {
        "wallet": Web3.to_checksum_address(wallet),
        "intentId": intent_id,
        "platformHash": PLATFORM_HASHES[platform],
        "postCommitment": commitment,
        "rewardAmount": int(reward_amount_wei),
        "fee": SUBMISSION_FEE_WEI,
        "deadline": deadline,
    }
    # Web3.to_hex (not HexBytes.hex) so the value keeps its 0x prefix; ethers v6
    # rejects unprefixed hex for the bytes ownerSignature argument.
    signature = Web3.to_hex(
        Account.sign_typed_data(
            private_key=_private_key(),
            domain_data=domain,
            message_types=message_types,
            message_data=message,
        ).signature
    )
    return {
        "intent_id": Web3.to_hex(intent_id),
        "platform_hash": Web3.to_hex(PLATFORM_HASHES[platform]),
        "post_commitment": Web3.to_hex(commitment),
        "fee_wei": str(SUBMISSION_FEE_WEI),
        "reward_amount_wei": str(reward_amount_wei),
        "deadline": deadline,
        "owner_signature": signature,
        "contract_address": context.contract_address,
        "token_address": context.token_address,
        "chain_id": CHAIN_ID,
    }


def verify_payment_receipt(tx_hash: str, expected: dict[str, Any], *, context: DailyTaskContractContext | None = None) -> dict[str, Any]:
    context = context or get_contract_context()
    try:
        tx_hash_bytes = Web3.to_bytes(hexstr=tx_hash)
        receipt = context.w3.eth.get_transaction_receipt(tx_hash_bytes)
        tx = context.w3.eth.get_transaction(tx_hash_bytes)
        if int(receipt["status"]) != 1:
            raise DailyTaskContractError("Payment transaction reverted")
        if Web3.to_checksum_address(tx["to"]) != context.contract_address:
            raise DailyTaskContractError("Payment transaction was sent to a different contract")
        if Web3.to_checksum_address(tx["from"]).lower() != Web3.to_checksum_address(expected["wallet"]).lower():
            raise DailyTaskContractError("Payment transaction sender does not match the signed-in wallet")
        if int(context.w3.eth.chain_id) != CHAIN_ID:
            raise DailyTaskContractError("Payment transaction was not verified on Celo Mainnet")
        decoded = context.contract.events.SubmissionFeePaid().process_receipt(receipt, errors="DISCARD")
        for log in decoded:
            args = log["args"]
            if (
                Web3.to_checksum_address(args["wallet"]).lower() == Web3.to_checksum_address(expected["wallet"]).lower()
                and Web3.to_hex(args["intentId"]).lower() == str(expected["intent_id"]).lower()
                and Web3.to_hex(args["platformHash"]).lower() == str(expected["platform_hash"]).lower()
                and Web3.to_hex(args["postCommitment"]).lower() == str(expected["post_commitment"]).lower()
                and int(args["fee"]) == SUBMISSION_FEE_WEI
                and int(args["rewardAmount"]) == int(expected["reward_amount_wei"])
            ):
                block = context.w3.eth.get_block(receipt["blockNumber"])
                if int(block["timestamp"]) > int(expected["deadline"]):
                    raise DailyTaskContractError("Payment voucher had expired before it was mined")
                return {"tx_hash": Web3.to_hex(receipt["transactionHash"]), "block_number": int(receipt["blockNumber"])}
        raise DailyTaskContractError("Payment transaction does not contain the expected fee event")
    except DailyTaskContractError:
        raise
    except Exception as exc:
        raise DailyTaskContractError("Unable to verify the Daily Task payment transaction") from exc


def disburse_reward_sync(wallet: str, amount: Any, task_id: str, platform: str) -> dict[str, Any]:
    """Send an approved submission's snapshotted reward from the on-chain pool."""
    if platform not in PLATFORM_HASHES:
        return {"success": False, "error": "Unsupported Daily Task platform"}
    try:
        context = get_contract_context()
        from eth_abi import encode as abi_encode
        reward_wei = reward_to_wei(amount)
        task_bytes = Web3.keccak(text=f"kwentofi-daily-task:{platform}:{task_id}")
        already_done = context.contract.functions.processedTasks(
            Web3.keccak(abi_encode(["address", "bytes32", "bytes32"], [Web3.to_checksum_address(wallet), task_bytes, PLATFORM_HASHES[platform]]))
        ).call()
        if already_done:
            return {"success": True, "already_disbursed": True, "tx_hash": None, "message": "Reward was already disbursed on-chain."}
        function = context.contract.functions.disburseReward(
            Web3.to_checksum_address(wallet), reward_wei, task_bytes, PLATFORM_HASHES[platform]
        )
        nonce = context.w3.eth.get_transaction_count(context.account.address, "pending")
        gas_price = int(context.w3.eth.gas_price * 1.2)
        gas = function.estimate_gas({"from": context.account.address})
        transaction = function.build_transaction({
            "from": context.account.address,
            "nonce": nonce,
            "chainId": CHAIN_ID,
            "gas": int(gas * 1.2),
            "gasPrice": gas_price,
        })
        signed = context.account.sign_transaction(transaction)
        tx_hash = context.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = context.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if int(receipt["status"]) != 1:
            return {"success": False, "error": "Daily Task reward transaction reverted", "tx_hash": Web3.to_hex(tx_hash)}
        return {"success": True, "tx_hash": Web3.to_hex(tx_hash)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
