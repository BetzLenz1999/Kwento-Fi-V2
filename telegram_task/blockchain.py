
from env_utils import get_env_int
import os
import logging
from web3 import Web3

logger = logging.getLogger(__name__)

class TelegramTaskBlockchain:
    """Telegram Task reward disbursement through DailyTaskRewardsV2."""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = get_env_int('CHAIN_ID', 42220)
        # Bounded, non-probing provider: this object is built at import time
        # (module-level singleton below), so it must not touch the network.
        from chain_rpc import make_provider
        self.w3 = Web3(make_provider(self.celo_rpc_url))

        logger.info("Telegram Task Blockchain Service initialized (DailyTaskRewardsV2 mode)")

    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def disburse_telegram_reward(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Disburse an approved Telegram reward from the v2 contract pool."""
        import asyncio
        return await asyncio.to_thread(
            self.disburse_telegram_reward_sync, wallet_address, amount, task_id
        )

    def disburse_telegram_reward_sync(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Pay through the configured owner-only DailyTaskRewardsV2 contract."""
        if task_id is None:
            return {"success": False, "error": "A unique task submission ID is required"}
        try:
            from daily_task_contract_service import disburse_reward_sync
            return disburse_reward_sync(wallet_address, amount, str(task_id), "telegram")
        except Exception as exc:
            logger.exception("Daily Task v2 Telegram reward disbursement failed")
            return {"success": False, "error": str(exc)}


# Global instance
telegram_blockchain_service = TelegramTaskBlockchain()
