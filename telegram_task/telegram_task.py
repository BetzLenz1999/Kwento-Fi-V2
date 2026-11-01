import os
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from config import DAILY_TASK_COOLDOWN_HOURS
from supabase_client import get_supabase_client
from cache_utils import supabase_cache
from referral_program.referral_service import current_origin_domain

logger = logging.getLogger(__name__)


def _coprime_stride(total: int, count: int) -> int:
    """Smallest stride >= total/count that is coprime with total.

    A stride coprime with `total` makes k -> (k * stride) % total injective, so
    the first `count` indices are all distinct while sweeping the whole
    opener x middle x closer space instead of clustering on a prefix (the old
    `i % n`, `(i//10) % n`, `(i//100) % n` layout exposed only a tenth of the
    closer pool, leaving the rest as dead code).
    """
    stride = max(1, total // count)
    while math.gcd(stride, total) != 1:
        stride += 1
    return stride


def _build_message_pool(openers, middles, closers, render, count: int = 1000):
    """Combine three phrase pools into `count` distinct messages.

    Each message is a unique (opener, middle, closer) triple. Pools must
    multiply to at least `count` so every generated message is distinct.
    """
    n_openers, n_middles, n_closers = len(openers), len(middles), len(closers)
    total = n_openers * n_middles * n_closers
    if total < count:
        raise ValueError(
            f"message pools too small: {total} combinations < {count} requested"
        )
    stride = _coprime_stride(total, count)
    pool = []
    for i in range(count):
        idx = (i * stride) % total
        s1 = openers[idx % n_openers]
        s2 = middles[(idx // n_openers) % n_middles]
        s3 = closers[(idx // (n_openers * n_middles)) % n_closers]
        pool.append(render(s1, s2, s3))
    return pool


def _wallet_filter(query, wallet_address: str):
    """Case-insensitive wallet_address match.

    Web sessions store checksummed addresses (Web3.to_checksum_address) while
    the Telegram bot stores lowercase — a case-sensitive .eq() made rows
    written from one surface invisible to the other. Addresses are hex-only,
    so ilike without wildcards is a safe case-insensitive equality.
    """
    return query.ilike('wallet_address', wallet_address)


# Preloaded custom messages (generated once at import time)
_TELEGRAM_MESSAGES: List[str] = []

def _generate_telegram_messages() -> List[str]:
    """Generate 1000 unique custom messages for Telegram (3 sentences each)

    Copy rules — these are posted by real users from personal accounts, not
    brand ads:
      * First-person, plain English. Brand taglines published verbatim from a
        personal account read as coordinated spam.
      * No claim of official affiliation (GoodMarket is an independent
        community project — see the impersonation-risk note in AGENTS.md).
      * No "free"/"guaranteed"/"join thousands" style promises.
      * Exactly one link per post (the referral link injected at serve time);
        the opening and middle sentences stay link-free.
      * Single closing punctuation only, no doubled "?." / ".!".
    """
    opening_phrases = [
        # Personal UBI learning and daily participation
        "I spent a few minutes learning how GoodDollar UBI works today.",
        "I am back for another short lesson about universal basic income.",
        "I claimed my G$ and read one more thing about the UBI model.",
        "I like that GoodDollar makes UBI easier for me to understand.",
        "I am learning how digital tools can support a wider UBI community.",
        "I finished today's GoodDollar learning task and took a note for later.",
        "I am keeping a small daily habit of learning about UBI and G$.",
        "I finally connected today's lesson with the idea of financial inclusion.",
        "I used today's task to learn where GoodDollar fits in the UBI conversation.",
        "I am still learning, but the GoodDollar UBI idea is becoming clearer.",
        "I found another simple explanation of how UBI can reach more people.",
        "I am taking my time with the lessons instead of rushing through them.",
        "I learned something new about G$ and the people building UBI communities.",
        "I am using today's task as a reason to keep exploring UBI.",
        "I like learning about UBI through short conversations and simple examples.",
        "I checked in today to keep learning with the GoodDollar community.",
        "I am curious how other members understand UBI in their own countries.",
        "I finished a task and now I want to hear another member's UBI perspective.",
        "I am learning that UBI is also about participation, not only payments.",
        "I came for the daily task and stayed interested in the wider UBI discussion.",
        "I am turning my daily G$ routine into a better understanding of UBI.",
        "I read a short lesson about GoodDollar and shared the idea with a friend.",
        "I am learning how community feedback can improve conversations about UBI.",
        "I like seeing different members ask practical questions about GoodDollar UBI.",
        "I am taking another small step toward understanding inclusive digital money.",
        "I finished today's task and thought about what fair access could look like.",
        "I am learning why community conversations matter to the future of UBI.",
        "I checked my G$ and then went back to the lesson about universal basic income.",
        "I am keeping my daily routine connected to something bigger than a token.",
        "I want to understand UBI well enough to explain it clearly to my friends.",
        "I learned how GoodDollar gives members a starting point for UBI discussions.",
        "I am comparing what I learn here with the questions people ask in my community.",
        "I am glad today's task gave me a reason to talk about financial inclusion.",
        "I am following the UBI conversation one short GoodDollar task at a time.",
        "I finished another task and kept thinking about how members can learn together.",
        "I am interested in the human side of UBI, not just the technology.",
        "I am learning how shared knowledge can make GoodDollar easier for newcomers.",
        "I checked in today and found another question worth discussing with members.",
        "I am building a better UBI vocabulary through these daily lessons.",
        "I like that my G$ routine can lead to thoughtful conversations about inclusion.",
        "I am learning how people in different places think about a basic income.",
        "I finished today's task and sent a friend a question about UBI.",
        "I am staying curious about GoodDollar and the community around its UBI idea.",
        "I learned one useful thing today about access, identity, and UBI.",
        "I am using my daily task to connect with people who are also exploring UBI.",
        "I like learning from members who bring different experiences to the discussion.",
        "I am back to learn, ask questions, and understand GoodDollar a little better.",
        "I finished a short lesson and now the UBI conversation feels more practical.",
        "I am keeping the focus on learning, sharing, and making UBI easier to discuss.",
    ]

    middle_phrases = [
        # Link-free by design: the only URL in the final post is the referral
        # link injected at serve time.
        "I like having a simple place to learn and compare notes with other members.",
        "The short lessons give me something useful to discuss with friends.",
        "I am learning at my own pace and asking questions when an idea is unclear.",
        "The UBI conversation makes more sense when members share real experiences.",
        "I enjoy hearing how people in different places think about financial inclusion.",
        "The daily task is small, but the conversations can lead to bigger ideas.",
        "I am learning that community knowledge is part of making UBI understandable.",
        "The lessons help me explain G$ without pretending to know everything.",
        "I like connecting with members who are curious rather than trying to sound expert.",
        "I am collecting practical questions about identity, access, and universal income.",
        "The best part is finding another member who asks the same question I had.",
        "I can take part for a few minutes and return when I have more time to learn.",
        "I am seeing how education can make a new financial system less intimidating.",
        "The community keeps the UBI idea grounded in everyday experiences.",
        "I am learning why inclusive design matters when digital money reaches new users.",
        "A short lesson often gives me a better question to bring to the community.",
        "I like that members can learn from one another without needing a crypto background.",
        "The conversation is more useful when people share what they actually understand.",
        "I am staying involved because learning with others feels more encouraging.",
        "The G$ routine gives me a regular reason to revisit the UBI discussion.",
        "I am finding new ways to describe UBI in plain language.",
        "The community helps me connect the technology with people and real needs.",
        "I like reading different viewpoints before forming my own opinion about UBI.",
        "I am learning that small questions can lead to thoughtful member conversations.",
        "The lessons are easier to remember when I talk them through with someone else.",
        "I am interested in how GoodDollar members use learning to support one another.",
        "The daily rhythm keeps me curious without making the topic feel overwhelming.",
        "I am learning to separate the idea of UBI from hype about quick money.",
        "The community side makes the learning feel more human and less technical.",
        "I like when a member explains a difficult idea using an everyday example.",
        "I am making room for different opinions while I learn what UBI could mean.",
        "The lessons and member discussions together give me a clearer picture.",
        "I am more comfortable asking questions because everyone starts somewhere.",
        "The point for me is learning, participating, and helping the next person understand.",
        "I am finding that financial inclusion starts with conversations people can follow.",
        "A few minutes of learning can turn into a useful exchange with another member.",
    ]

    closing_phrases = [
        "If you want to start, this is the link I used: https://goodmarket.live",
        "My link, in case it helps: https://goodmarket.live",
        "I started here: https://goodmarket.live",
        "This is where I claim mine: https://goodmarket.live",
        "Same place I use every day: https://goodmarket.live",
        "Here is where I started learning: https://goodmarket.live",
        "Start with the daily task here: https://goodmarket.live",
        "The site I use for this: https://goodmarket.live",
        "You can try it here: https://goodmarket.live",
        "Link for anyone curious: https://goodmarket.live",
    ]

    def render(s1, s2, s3):
        return f"{s1}\n\n{s2}\n\n{s3}"

    return _build_message_pool(
        opening_phrases, middle_phrases, closing_phrases, render, count=1000
    )



# Generate messages once at module load
_TELEGRAM_MESSAGES = _generate_telegram_messages()


class TelegramTaskService:
    def __init__(self):
        self.supabase = get_supabase_client()
        # Reward amount is now dynamic and fetched from the reward configuration service
        # self.task_reward = 100.0  # 100 G$ reward - REMOVED, replaced by get_task_reward()

        # Use preloaded messages from module level (generated once at import)
        self.custom_messages = _TELEGRAM_MESSAGES

        self.telegram_channel = "GoodDollarX"
        self.cooldown_hours = DAILY_TASK_COOLDOWN_HOURS

        logger.info("📱 Telegram Task Service initialized")
        # logger.info(f"💰 Reward: {self.task_reward} G$") # REMOVED - dynamic reward
        logger.info(f"📢 Channel: t.me/{self.telegram_channel}")
        logger.info(f"⏰ Cooldown: {self.cooldown_hours} hours")
        logger.info(f"💬 Custom Messages: {len(self.custom_messages)} unique variations (wallet + day based rotation ensures unique messages per user)")



    def _create_tables(self):
        """Create necessary database tables (run this in Supabase SQL editor)"""
        sql_commands = """
        -- Telegram task completion log
        CREATE TABLE IF NOT EXISTS telegram_task_log (
            id SERIAL PRIMARY KEY,
            wallet_address VARCHAR(42) NOT NULL,
            telegram_url TEXT NOT NULL,
            reward_amount DECIMAL(18,8) NOT NULL,
            transaction_hash VARCHAR(66) NOT NULL,
            status VARCHAR(20) DEFAULT 'completed',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(telegram_url)
        );

        CREATE INDEX IF NOT EXISTS idx_telegram_task_wallet ON telegram_task_log(wallet_address);
        CREATE INDEX IF NOT EXISTS idx_telegram_task_created ON telegram_task_log(created_at);

        ALTER TABLE telegram_task_log ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "Allow all operations on telegram_task_log" ON telegram_task_log FOR ALL USING (true);
        """
        logger.info("📋 Telegram task database tables ready (run SQL commands in Supabase)")

    def _mask_wallet(self, wallet_address: str) -> str:
        """Mask wallet address for display"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    def get_custom_message_for_user(self, wallet_address: str) -> str:
        """Get custom message for the user - wallet-based rotation ensures unique messages

        Each user gets a different message every day based on:
          1. Their wallet address (for uniqueness per user)
          2. The current UTC day (daily rotation)

        The hour is deliberately NOT part of the index: a message shown at
        7:59 used to differ from the one the user actually posted at 8:01, so
        the admin reviewing the submission saw text that did not match the
        post. Keying on (wallet, day) keeps the message stable all day.
        """
        import hashlib
        from datetime import datetime, timezone

        # Normalize wallet address to lowercase
        wallet_normalized = wallet_address.lower().strip()

        # Hash wallet address to get consistent index
        wallet_hash = int(hashlib.sha256(wallet_normalized.encode()).hexdigest(), 16)

        # Get current UTC time for rotation
        now_utc = datetime.now(timezone.utc)
        day_of_year = now_utc.timetuple().tm_yday

        # Use multiple factors for better distribution:
        # 1. Wallet hash (unique per user)
        # 2. Day of year (daily rotation)
        # 3. Last 4 chars of wallet (additional entropy)
        last_4_chars = int(wallet_normalized[-4:], 16) if len(wallet_normalized) >= 4 else 0

        # Combine all factors for unique message index
        message_index = (
            wallet_hash +
            (day_of_year * 37) +  # Prime number multiplier
            (last_4_chars * 7)     # Prime number multiplier
        ) % len(self.custom_messages)

        logger.info(f"📅 Message index {message_index} for user: {wallet_address[:8]}... (Day: {day_of_year}, {len(self.custom_messages)} unique messages available)")

        message = self.custom_messages[message_index]

        # Re-anchor hardcoded 'goodmarket.live' (any case) to the origin the
        # requesting client actually used; fallback is referral_service BASE_URL.
        try:
            domain = current_origin_domain()
            if domain != 'goodmarket.live':
                message = re.sub(r'goodmarket\.live', lambda _: domain, message, flags=re.IGNORECASE)
        except Exception as anchor_err:
            logger.warning(f"⚠️ Could not re-anchor message domain: {anchor_err}")
        return message

    def _validate_telegram_url(self, telegram_url: str) -> Dict[str, Any]:
        """Validate Telegram post URL and verify post existence via Telegram Bot API"""
        try:
            telegram_url = telegram_url.strip()

            if not telegram_url:
                return {"valid": False, "error": "Telegram post URL is required"}

            # Valid formats: https://t.me/GoodDollarX/123 or https://telegram.me/GoodDollarX/123
            if not (telegram_url.startswith("https://t.me/") or
                   telegram_url.startswith("https://telegram.me/")):
                return {"valid": False, "error": "Please provide a valid Telegram post URL (https://t.me/...)"}

            # Check if URL contains the expected channel
            if f"/{self.telegram_channel}/" not in telegram_url:
                return {"valid": False, "error": f"Post must be in t.me/{self.telegram_channel} channel"}

            # Check if URL contains a message ID (number after channel name)
            url_parts = telegram_url.split('/')
            if len(url_parts) < 5 or not url_parts[-1].isdigit():
                return {"valid": False, "error": "URL must be a direct link to your Telegram post (should end with a message number)"}

            # Extract message ID
            message_id = int(url_parts[-1])

            # Minimum message ID validation - real posts in GoodDollarX are 6+ digits
            if message_id < 200000:
                return {"valid": False, "error": "Invalid post link. Please provide a real Telegram post URL from t.me/GoodDollarX channel"}

            # Additional check: reject common test numbers
            test_numbers = [123, 1234, 12345, 123456, 1234567]
            if message_id in test_numbers:
                return {"valid": False, "error": "Please provide a real Telegram post link, not a test URL"}

            # CRITICAL: Verify post exists using Telegram Web API (NO BOT TOKEN NEEDED)
            try:
                import requests
                from bs4 import BeautifulSoup

                # Access Telegram post via public web interface
                # This works for public channels without authentication
                web_url = f"https://t.me/{self.telegram_channel}/{message_id}?embed=1"

                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }

                logger.info(f"🔍 Verifying post existence: {web_url}")

                response = requests.get(web_url, headers=headers, timeout=10, allow_redirects=False)

                # Check response status
                if response.status_code == 200:
                    # Post exists! Verify it's actually a post page
                    if 'tgme_widget_message' in response.text or 'message' in response.text.lower():
                        logger.info(f"✅ Telegram post {message_id} verified as existing")
                    else:
                        logger.warning(f"⚠️ URL exists but doesn't appear to be a valid post")
                        return {"valid": False, "error": "Invalid post URL. Please provide a real Telegram post link."}

                elif response.status_code == 404:
                    logger.warning(f"❌ Post {message_id} does not exist (404)")
                    return {"valid": False, "error": "This post does not exist. Please create a real post and submit the correct link."}

                elif response.status_code in [301, 302, 307, 308]:
                    # Redirects might indicate channel issues
                    logger.warning(f"⚠️ Post URL redirected (status {response.status_code})")
                    return {"valid": False, "error": "Invalid post link. Please verify you're using the correct channel."}

                else:
                    logger.warning(f"⚠️ Unexpected status code {response.status_code}")
                    # Don't block on unexpected errors, allow through
                    pass

            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Telegram verification timeout - allowing request")
                # Don't block user if verification times out
                pass

            except Exception as verify_error:
                logger.warning(f"⚠️ Post verification failed: {verify_error}")
                # Don't block user if verification fails
                pass

            return {"valid": True, "telegram_url": telegram_url}

        except Exception as e:
            logger.error(f"❌ Telegram URL validation error: {e}")
            return {"valid": False, "error": "Validation failed. Please try again."}

    async def check_eligibility(self, wallet_address: str) -> Dict[str, Any]:
        """Check if user can claim Telegram task reward"""
        try:
            if not self.supabase:
                return {
                    'can_claim': True,
                    'reason': 'Database not available'
                }

            logger.info(f"🔍 Checking Telegram eligibility for {wallet_address[:8]}...")

            # Check for pending submission (waiting for approval)
            # Cooldown starts IMMEDIATELY after submission, not after approval
            pending_check = _wallet_filter(
                self.supabase.table('telegram_task_log')\
                    .select('created_at, status'),
                wallet_address,
            )\
                .eq('status', 'pending')\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Pending check result: {len(pending_check.data) if pending_check.data else 0} pending submissions")

            if pending_check.data:
                # Cooldown active - submission is pending
                pending_time = datetime.fromisoformat(pending_check.data[0]['created_at'].replace('Z', '+00:00'))
                next_claim_time = pending_time + timedelta(hours=self.cooldown_hours)

                logger.info(f"⏰ Cooldown active (pending) - Submitted: {pending_time}, Next available: {next_claim_time}")

                return {
                    'can_claim': False,
                    'has_pending_submission': True,
                    'reason': 'Waiting for admin approval',
                    'status': 'pending',
                    'next_claim_time': next_claim_time.isoformat(),
                    'last_claim': pending_time.isoformat()
                }

            # Check the last COMPLETED or REJECTED claim within the cooldown window
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.cooldown_hours)
            last_claim = _wallet_filter(
                self.supabase.table('telegram_task_log')\
                    .select('created_at, status'),
                wallet_address,
            )\
                .in_('status', ['completed', 'rejected'])\
                .gte('created_at', cutoff_time.isoformat())\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Recent claims (last {self.cooldown_hours}h): {len(last_claim.data) if last_claim.data else 0}")
            if last_claim.data:
                logger.info(f"🔍 Last claim: {last_claim.data[0]}")

            if last_claim.data:
                last_claim_status = last_claim.data[0]['status']

                # If last claim was REJECTED, user can resubmit immediately
                if last_claim_status == 'rejected':
                    logger.info(f"✅ Last submission was rejected - user can resubmit")
                    return {
                        'can_claim': True,
                        'reward_amount': self.get_task_reward() # Fetch dynamic reward
                    }

                # If last claim was COMPLETED, cooldown is active
                if last_claim_status == 'completed':
                    last_claim_time = datetime.fromisoformat(last_claim.data[0]['created_at'].replace('Z', '+00:00'))
                    next_claim_time = last_claim_time + timedelta(hours=self.cooldown_hours)

                    logger.info(f"⏰ Cooldown active (completed) - Last claim: {last_claim_time}, Next available: {next_claim_time}")

                    return {
                        'can_claim': False,
                        'reason': 'Already claimed today',
                        'next_claim_time': next_claim_time.isoformat(),
                        'last_claim': last_claim_time.isoformat()
                    }

            logger.info(f"✅ User can claim - no recent submissions")

            return {
                'can_claim': True,
                'reward_amount': self.get_task_reward() # Fetch dynamic reward
            }

        except Exception as e:
            logger.error(f"❌ Error checking Telegram task eligibility: {e}")
            return {
                'can_claim': True,
                'reason': 'Error checking eligibility'
            }

    async def claim_task_reward(self, wallet_address: str, post_url: str) -> Dict[str, Any]:
        """Reject legacy direct inserts; new submissions require verified v2 payment."""
        return {
            'success': False,
            'error': 'Daily Task submissions require the 20 G$ on-chain anti-spam payment. Submit through Community.',
            'code': 'payment_required',
        }

    async def approve_submission(self, submission_id: int, admin_wallet: str) -> Dict[str, Any]:
        """Admin approves a submission and disburses reward"""
        try:
            if not self.supabase:
                return {'success': False, 'error': 'Database not available'}

            # Get submission details
            submission = self.supabase.table('telegram_task_log')\
                .select('*')\
                .eq('id', submission_id)\
                .eq('status', 'pending')\
                .execute()

            if not submission.data or len(submission.data) == 0:
                return {'success': False, 'error': 'Submission not found or already processed'}

            sub_data = submission.data[0]
            if sub_data.get('payment_status') not in ('paid', 'grandfathered'):
                return {'success': False, 'error': 'This submission has no verified Daily Task payment and cannot be rewarded.'}
            wallet_address = sub_data['wallet_address']
            telegram_url = sub_data['telegram_url']
            reward_amount = sub_data['reward_amount'] # Use the reward amount stored in the submission

            logger.info(f"✅ Admin {admin_wallet[:8]}... approving submission {submission_id}")

            # Disburse reward
            from telegram_task.blockchain import telegram_blockchain_service

            disbursement = telegram_blockchain_service.disburse_telegram_reward_sync(
                wallet_address=wallet_address,
                amount=reward_amount,
                task_id=str(submission_id)
            )

            if disbursement.get('success'):
                # Update status to completed
                self.supabase.table('telegram_task_log').update({
                    'status': 'completed',
                    'transaction_hash': disbursement.get('tx_hash'),
                    'approved_by': admin_wallet,
                    'approved_at': datetime.now(timezone.utc).isoformat()
                }).eq('id', submission_id).execute()

                logger.info(f"✅ Telegram task approved and disbursed: {reward_amount} G$ to {self._mask_wallet(wallet_address)}")

                return {
                    'success': True,
                    'tx_hash': disbursement.get('tx_hash'),
                    'message': f'Approved! {reward_amount} G$ disbursed to user.'
                }
            else:
                # Keep status as 'pending' so admin can retry — only move out of pending on success
                logger.error(f"❌ Disbursement failed for submission {submission_id}: {disbursement.get('error')} — keeping as pending for retry")

                return {
                    'success': False,
                    'error': f"Disbursement failed: {disbursement.get('error')}. Submission kept as pending — please retry approval."
                }

        except Exception as e:
            logger.error(f"❌ Approval error: {e}")
            return {'success': False, 'error': str(e)}

    async def reject_submission(self, submission_id: int, admin_wallet: str, reason: str = '') -> Dict[str, Any]:
        """Admin rejects a submission - cooldown is reset, user can immediately resubmit"""
        try:
            if not self.supabase:
                return {'success': False, 'error': 'Database not available'}

            # Get submission details first
            submission = self.supabase.table('telegram_task_log')\
                .select('wallet_address')\
                .eq('id', submission_id)\
                .eq('status', 'pending')\
                .execute()

            if not submission.data:
                return {'success': False, 'error': 'Submission not found or already processed'}

            wallet_address = submission.data[0]['wallet_address']

            # Update status to rejected - this effectively resets the cooldown
            result = self.supabase.table('telegram_task_log').update({
                'status': 'rejected',
                'rejected_by': admin_wallet,
                'rejected_at': datetime.now(timezone.utc).isoformat(),
                'rejection_reason': reason
            }).eq('id', submission_id).eq('status', 'pending').execute()

            if result.data:
                logger.info(f"❌ Admin {admin_wallet[:8]}... rejected submission {submission_id}")
                logger.info(f"✅ Cooldown reset for {wallet_address[:8]}... - User can resubmit immediately")

                return {
                    'success': True,
                    'message': 'Submission rejected. Cooldown has been reset - user can resubmit immediately with a new post.',
                    'cooldown_reset': True
                }
            else:
                return {'success': False, 'error': 'Submission not found or already processed'}

        except Exception as e:
            logger.error(f"❌ Rejection error: {e}")
            return {'success': False, 'error': str(e)}

    async def get_task_stats(self, wallet_address: str) -> Dict[str, Any]:
        """Get user's Telegram task statistics"""
        try:
            if not self.supabase:
                return {
                    'total_earned': 0,
                    'total_claims': 0,
                    'can_claim_today': True
                }

            # Get total earned
            claims = _wallet_filter(
                self.supabase.table('telegram_task_log')\
                    .select('reward_amount'),
                wallet_address,
            ).execute()

            total_earned = sum(float(c.get('reward_amount', 0)) for c in claims.data or [])
            total_claims = len(claims.data or [])

            # Check if can claim today
            eligibility = await self.check_eligibility(wallet_address)

            return {
                'total_earned': total_earned,
                'total_claims': total_claims,
                'can_claim_today': eligibility.get('can_claim', False),
                'next_claim_time': eligibility.get('next_claim_time'),
                'reward_amount': self.get_task_reward() # Fetch dynamic reward
            }

        except Exception as e:
            logger.error(f"❌ Error getting Telegram task stats: {e}")
            return {
                'total_earned': 0,
                'total_claims': 0,
                'can_claim_today': True
            }

    def get_transaction_history(self, wallet_address: str, limit: int = 50) -> Dict[str, Any]:
        """Get user's Telegram task transaction history"""
        try:
            if not self.supabase:
                return {
                    'success': True,
                    'transactions': [],
                    'total_count': 0,
                    'total_earned': 0
                }

            logger.info(f"📋 Getting Telegram task history for {wallet_address[:8]}... (limit: {limit})")

            # Get transaction history
            history = _wallet_filter(
                self.supabase.table('telegram_task_log')\
                    .select('*'),
                wallet_address,
            )\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute()

            transactions = []
            total_earned = 0

            if history.data:
                for record in history.data:
                    reward_amount = float(record.get('reward_amount', 0))
                    total_earned += reward_amount

                    transactions.append({
                        'id': record.get('id'),
                        'reward_amount': reward_amount,
                        'transaction_hash': record.get('transaction_hash'),
                        'telegram_url': record.get('telegram_url'),
                        'status': record.get('status', 'completed'),
                        'created_at': record.get('created_at'),
                        'explorer_url': f"https://explorer.celo.org/mainnet/tx/{record.get('transaction_hash')}" if record.get('transaction_hash') else None,
                        'rejection_reason': record.get('rejection_reason')
                    })

            logger.info(f"✅ Retrieved {len(transactions)} Telegram task transactions for {wallet_address[:8]}... (Total: {total_earned} G$)")

            return {
                'success': True,
                'transactions': transactions,
                'total_count': len(transactions),
                'total_earned': total_earned,
                'summary': {
                    'total_earned': total_earned,
                    'transaction_count': len(transactions),
                    'avg_reward': total_earned / len(transactions) if transactions else 0
                }
            }

        except Exception as e:
            logger.error(f"❌ Error getting Telegram task transaction history: {e}")
            return {
                'success': False,
                'error': str(e),
                'transactions': [],
                'total_count': 0,
                'total_earned': 0
            }

    def get_task_reward(self) -> float:
        """
        Fetches the current reward amount from the reward configuration service.
        This ensures that the reward amount is dynamic and can be changed by admins
        without code redeployment.
        """
        try:
            from reward_config_service import RewardConfigService
            reward_service = RewardConfigService()
            reward_amount = reward_service.get_reward_amount('telegram_task')
            logger.info(f"💰 Fetched dynamic reward amount for Telegram task: {reward_amount} G$")
            return reward_amount
        except Exception as e:
            logger.error(f"❌ Failed to fetch dynamic reward amount: {e}. Falling back to default.")
            # Fallback to a default value if fetching fails
            return 100.0 # Default reward amount

# Global instance
telegram_task_service = TelegramTaskService()

def init_telegram_task(app):
    """Initialize Telegram Task system with Flask app"""
    try:
        logger.info("📱 Initializing Telegram Task system...")

        from flask import session, request, jsonify

        @app.route('/api/telegram-task/status', methods=['GET'])
        def get_telegram_task_status():
            """Get Telegram task status for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    stats = loop.run_until_complete(
                        telegram_task_service.get_task_stats(wallet_address)
                    )
                finally:
                    loop.close()

                return jsonify(stats), 200

            except Exception as e:
                logger.error(f"❌ Telegram task status error: {e}")
                return jsonify({'error': 'Failed to get task status'}), 500

        @app.route('/api/telegram-task/custom-message', methods=['GET'])
        def get_telegram_custom_message():
            """Get custom message for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                verified = session.get('verified')

                logger.info(f"📱 Custom message request - wallet: {wallet_address[:8] if wallet_address else 'None'}..., verified: {verified}")
                logger.info(f"📱 Session keys: {list(session.keys())}")

                if not wallet_address:
                    logger.warning(f"❌ No wallet address in session")
                    return jsonify({
                        'success': False,
                        'error': 'Not authenticated - no wallet'
                    }), 401

                if not verified:
                    logger.warning(f"❌ Wallet not verified")
                    return jsonify({
                        'success': False,
                        'error': 'Not authenticated - not verified'
                    }), 401

                # Get the custom message for this user
                custom_message = telegram_task_service.get_custom_message_for_user(wallet_address)

                logger.info(f"✅ Custom message generated for {wallet_address[:8]}... (length: {len(custom_message)})")

                return jsonify({
                    'success': True,
                    'custom_message': custom_message,
                    'wallet': wallet_address[:8] + "..."
                }), 200

            except Exception as e:
                logger.error(f"❌ Error getting custom message: {e}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to generate message',
                    'details': str(e)
                }), 500

        @app.route('/api/telegram-task/claim', methods=['POST'])
        def claim_telegram_task():
            """Claim Telegram task reward"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                data = request.get_json()
                telegram_url = data.get('telegram_url', '').strip()

                if not telegram_url:
                    return jsonify({
                        'success': False,
                        'error': 'Telegram post URL is required'
                    }), 400

                import asyncio

                # Use a fresh event loop to avoid conflicts
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    result = loop.run_until_complete(
                        telegram_task_service.claim_task_reward(wallet_address, telegram_url)
                    )
                finally:
                    try:
                        loop.close()
                    except:
                        pass

                if result.get('success'):
                    return jsonify(result), 200
                else:
                    return jsonify(result), 400

            except Exception as e:
                logger.error(f"❌ Telegram task claim error: {e}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")
                return jsonify({'error': 'Failed to claim task', 'details': str(e)}), 500

        @app.route('/api/telegram-task/history', methods=['GET'])
        def get_telegram_task_history():
            """Get Telegram task transaction history for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                limit = int(request.args.get('limit', 50))

                history = telegram_task_service.get_transaction_history(wallet_address, limit)

                return jsonify(history), 200

            except Exception as e:
                logger.error(f"❌ Telegram task history error: {e}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to get transaction history',
                    'transactions': [],
                    'total_count': 0
                }), 500

        logger.info("✅ Telegram Task system initialized successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to initialize Telegram Task system: {e}")
        return False
