import os
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
from config import DAILY_TASK_COOLDOWN_HOURS
from supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


def _coprime_stride(total: int, count: int) -> int:
    """Smallest stride >= total/count that is coprime with total.

    A stride coprime with `total` makes k -> (k * stride) % total injective,
    so the first `count` indices are all distinct while sweeping the whole
    opener x middle x closer space instead of clustering on a prefix.
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


class TwitterTaskService:
    def __init__(self):
        self.supabase = get_supabase_client()

        # Custom messages for Twitter posts (keeps @GoodDollarTeam @gooddollarorg mentions)
        self.custom_messages = self._generate_custom_messages()

        self.cooldown_hours = DAILY_TASK_COOLDOWN_HOURS

        logger.info("🐦 Twitter Task Service initialized")
        logger.info(f"⏰ Cooldown: {self.cooldown_hours} hours")
        logger.info(f"💬 Custom Messages: {len(self.custom_messages)} unique variations (wallet + day based rotation ensures unique messages per user)")
        logger.info(f"💰 Rewards: Dynamic (loaded from admin configuration)")
    
    def get_task_reward(self) -> float:
        """Get current reward amount from configuration"""
        from reward_config_service import reward_config_service
        return reward_config_service.get_reward_amount('twitter_task')

    def _generate_custom_messages(self):
        """Generate 1000 unique custom messages for Twitter (respecting character limits)

        Copy rules — these posts are signed by real users from personal
        accounts, not brand ads:
          * First-person, plain English. Brand taglines published verbatim from
            a personal account read as coordinated spam to X and to other
            users.
          * No claim of official affiliation (GoodMarket is an independent
            community project — see the impersonation-risk note in AGENTS.md).
          * No "free"/"guaranteed"/"join thousands" style promises.
          * Exactly one link per post (the referral link injected at serve
            time); the middle sentence stays link-free so a post never carries
            three URLs.
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
            # @gooddollarorg is a factual reference to the project, not a claim
            # of affiliation. The second "@GoodDollarTeam" mention was dropped:
            # two mentions plus three URLs read as a coordinated campaign.
            return f"{s1} {s2}\n\n{s3} @gooddollarorg"

        return _build_message_pool(
            opening_phrases, middle_phrases, closing_phrases, render, count=1000
        )

    def _mask_wallet(self, wallet_address: str) -> str:
        """Mask wallet address for display"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    def get_custom_message_for_user(self, wallet_address: str) -> str:
        """Get custom message for the user - wallet-based rotation ensures unique messages with personal referral link.

        Rotation is keyed on (wallet, UTC day) only. The previous version also
        mixed in the current hour, which meant the message shown to a user at
        7:59 was different from the one they actually posted at 8:01 — the
        admin reviewing the submission saw text that did not match the post.

        Domain re-anchoring: the static pool text says 'goodmarket.live', but
        the referral link and bare-domain text are re-anchored to the origin
        the requesting client actually used (Vercel preview / custom domain),
        same convention as referral_service.build_referral_link."""
        import hashlib
        from datetime import datetime, timezone
        from referral_program.referral_service import (
            ReferralService, build_referral_link, current_origin_domain,
        )
        
        # Normalize wallet address to lowercase
        wallet_normalized = wallet_address.lower().strip()
        
        # Hash wallet address to get consistent index
        wallet_hash = int(hashlib.sha256(wallet_normalized.encode()).hexdigest(), 16)
        
        # Get current UTC time for rotation
        now_utc = datetime.now(timezone.utc)
        day_of_year = now_utc.timetuple().tm_yday
        
        # Use multiple factors for better distribution
        last_4_chars = int(wallet_normalized[-4:], 16) if len(wallet_normalized) >= 4 else 0
        
        # Combine all factors for unique message index
        message_index = (
            wallet_hash + 
            (day_of_year * 37) +  # Prime number multiplier
            (last_4_chars * 7)     # Prime number multiplier
        ) % len(self.custom_messages)
        
        # Get the base message template
        message = self.custom_messages[message_index]
        
        # Replace the static goodmarket.live URL with the user's personal referral link.
        # Origin-aware: build_referral_link uses flask.request.host_url on request.
        try:
            referral_service = ReferralService()
            referral_code = referral_service.generate_code_for_wallet(wallet_address)
            referral_link = build_referral_link(referral_code)
            message = message.replace("https://goodmarket.live", referral_link)
            logger.info(f"🔗 Referral link injected for {wallet_address[:8]}...: {referral_link}")
        except Exception as ref_err:
            logger.warning(f"⚠️ Could not generate referral link for {wallet_address[:8]}...: {ref_err}")

        # Re-anchor any remaining bare 'goodmarket.live' text (opening/middle
        # phrases) to the current origin domain. Lambda repl avoids re's
        # backreference escaping on domain strings containing digits.
        try:
            domain = current_origin_domain()
            if domain != 'goodmarket.live':
                message = re.sub(r'goodmarket\.live', lambda _: domain, message, flags=re.IGNORECASE)
        except Exception as anchor_err:
            logger.warning(f"⚠️ Could not re-anchor message domain: {anchor_err}")
        
        logger.info(f"📅 Message index {message_index} for user: {wallet_address[:8]}... (Day: {day_of_year}, {len(self.custom_messages)} unique messages available)")
        return message

    def _validate_twitter_url(self, twitter_url: str) -> Dict[str, Any]:
        """Validate Twitter post URL"""
        try:
            twitter_url = twitter_url.strip()

            if not twitter_url:
                return {"valid": False, "error": "Twitter post URL is required"}

            # Valid formats: https://twitter.com/user/status/123 or https://x.com/user/status/123
            if not (twitter_url.startswith("https://twitter.com/") or
                   twitter_url.startswith("https://x.com/")):
                return {"valid": False, "error": "Please provide a valid Twitter post URL (https://twitter.com/... or https://x.com/...)"}

            # Check if URL contains /status/ (tweet format)
            if "/status/" not in twitter_url:
                return {"valid": False, "error": "URL must be a direct link to your tweet (should contain /status/)"}

            # Extract tweet ID
            url_parts = twitter_url.split('/')
            status_index = url_parts.index('status')

            if len(url_parts) <= status_index + 1:
                return {"valid": False, "error": "Invalid tweet link format"}

            tweet_id = url_parts[status_index + 1].split('?')[0]  # Remove query params

            if not tweet_id.isdigit():
                return {"valid": False, "error": "Invalid tweet ID in URL"}

            # Minimum tweet ID validation (Twitter IDs are very long numbers)
            if len(tweet_id) < 10:
                return {"valid": False, "error": "Invalid tweet link. Please provide a real Twitter post URL"}

            # CRITICAL: Verify post exists using Twitter/X Web API (NO API TOKEN NEEDED)
            try:
                import requests

                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }

                logger.info(f"🔍 Verifying Twitter post existence: {twitter_url}")

                response = requests.get(twitter_url, headers=headers, timeout=5, allow_redirects=True)

                # Check response status
                if response.status_code == 200:
                    # Post exists! Basic verification passed
                    logger.info(f"✅ Twitter post verified as existing")
                elif response.status_code == 404:
                    logger.warning(f"❌ Post does not exist (404)")
                    return {"valid": False, "error": "This post does not exist. Please create a real post and submit the correct link."}
                else:
                    logger.warning(f"⚠️ Unexpected status code {response.status_code} - allowing for admin review")
                    # Don't block on unexpected errors, allow through
                    pass

            except requests.exceptions.Timeout:
                logger.warning(f"⚠️ Twitter verification timeout - allowing request for admin review")
                # Don't block user if verification times out
                pass

            except requests.exceptions.ConnectionError as conn_err:
                logger.warning(f"⚠️ Connection error during verification - allowing request: {conn_err}")
                # Don't block user on network issues
                pass

            except Exception as verify_error:
                logger.warning(f"⚠️ Post verification failed - allowing for admin review: {verify_error}")
                # Don't block user if verification fails
                pass

            return {"valid": True, "twitter_url": twitter_url}

        except Exception as e:
            logger.error(f"❌ Twitter URL validation error: {e}")
            return {"valid": False, "error": "Validation failed. Please try again."}

    async def check_eligibility(self, wallet_address: str) -> Dict[str, Any]:
        """Check if user can claim Twitter task reward - CACHED"""
        # Use 60-second cache for eligibility
        cache_key = f'twitter_elig_{wallet_address}'
        if hasattr(self, '_cache'):
            if cache_key in self._cache:
                cached_data, cached_time = self._cache[cache_key]
                import time
                if time.time() - cached_time < 60:  # 60 seconds
                    logger.info(f"📦 Using cached Twitter eligibility for {wallet_address[:8]}...")
                    return cached_data
        else:
            self._cache = {}
        
        try:
            if not self.supabase:
                return {
                    'can_claim': True,
                    'reason': 'Database not available'
                }

            logger.info(f"🔍 Checking Twitter eligibility for {wallet_address[:8]}...")

            # Check for pending submission (waiting for approval)
            pending_check = _wallet_filter(
                self.supabase.table('twitter_task_log')\
                    .select('created_at, status'),
                wallet_address,
            )\
                .eq('status', 'pending')\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Pending check result: {len(pending_check.data) if pending_check.data else 0} pending submissions")

            if pending_check.data:
                result = {
                    'can_claim': False,
                    'has_pending_submission': True,
                    'reason': 'Waiting for admin approval',
                    'status': 'pending'
                }
                
                # Cache pending status
                import time
                self._cache[cache_key] = (result, time.time())
                
                return result

            # Check last COMPLETED claim within the cooldown window (only approved submissions trigger cooldown)
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.cooldown_hours)
            last_claim = _wallet_filter(
                self.supabase.table('twitter_task_log')\
                    .select('created_at, status'),
                wallet_address,
            )\
                .eq('status', 'completed')\
                .gte('created_at', cutoff_time.isoformat())\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()

            logger.info(f"🔍 Completed claims (last {self.cooldown_hours}h): {len(last_claim.data) if last_claim.data else 0}")
            if last_claim.data:
                logger.info(f"🔍 Last completed claim: {last_claim.data[0]}")

            if last_claim.data:
                # User already has approved claim within cooldown window - cooldown active
                last_claim_time = datetime.fromisoformat(last_claim.data[0]['created_at'].replace('Z', '+00:00'))
                next_claim_time = last_claim_time + timedelta(hours=self.cooldown_hours)

                logger.info(f"⏰ Cooldown active - Last claim: {last_claim_time}, Next available: {next_claim_time}")

                result = {
                    'can_claim': False,
                    'reason': f'Already claimed within the last {self.cooldown_hours} hours',
                    'next_claim_time': next_claim_time.isoformat(),
                    'last_claim': last_claim_time.isoformat()
                }
                
                # Cache blocked result
                import time
                self._cache[cache_key] = (result, time.time())
                
                return result

            logger.info(f"✅ User can claim - no completed claims within cooldown window")

            result = {
                'can_claim': True,
                'reward_amount': self.get_task_reward()
            }
            
            # Cache the result
            import time
            self._cache[cache_key] = (result, time.time())
            
            return result

        except Exception as e:
            logger.error(f"❌ Error checking Twitter task eligibility: {e}")
            error_result = {
                'can_claim': True,
                'reason': 'Error checking eligibility'
            }
            
            # Cache error result too (prevent repeated failures)
            import time
            self._cache[cache_key] = (error_result, time.time())
            
            return error_result

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
            submission = self.supabase.table('twitter_task_log')\
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
            twitter_url = sub_data['twitter_url']

            logger.info(f"✅ Admin {admin_wallet[:8]}... approving submission {submission_id}")

            # Disburse reward (use the amount stored in the submission)
            from twitter_task.blockchain import twitter_blockchain_service

            # Preserve the exact DB decimal string through the contract
            # conversion; a binary float can round valid 18-decimal rewards.
            current_reward = str(sub_data['reward_amount'])
            disbursement = twitter_blockchain_service.disburse_twitter_reward_sync(
                wallet_address=wallet_address,
                amount=current_reward,
                task_id=str(submission_id)
            )

            if disbursement.get('success'):
                # Update status to completed
                self.supabase.table('twitter_task_log').update({
                    'status': 'completed',
                    'transaction_hash': disbursement.get('tx_hash'),
                    'approved_by': admin_wallet,
                    'approved_at': datetime.now(timezone.utc).isoformat()
                }).eq('id', submission_id).execute()

                logger.info(f"✅ Twitter task approved and disbursed: {current_reward} G$ to {self._mask_wallet(wallet_address)}")

                return {
                    'success': True,
                    'tx_hash': disbursement.get('tx_hash'),
                    'message': f'Approved! {current_reward} G$ disbursed to user.'
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
            submission = self.supabase.table('twitter_task_log')\
                .select('wallet_address')\
                .eq('id', submission_id)\
                .eq('status', 'pending')\
                .execute()

            if not submission.data:
                return {'success': False, 'error': 'Submission not found or already processed'}

            wallet_address = submission.data[0]['wallet_address']

            # Update status to rejected - this effectively resets the cooldown
            self.supabase.table('twitter_task_log').update({
                'status': 'rejected',
                'rejected_by': admin_wallet,
                'rejected_at': datetime.now(timezone.utc).isoformat(),
                'rejection_reason': reason
            }).eq('id', submission_id).eq('status', 'pending').execute()

            logger.info(f"❌ Admin {admin_wallet[:8]}... rejected submission {submission_id}")
            logger.info(f"✅ Cooldown reset for {wallet_address[:8]}... - User can resubmit immediately")

            return {
                'success': True,
                'message': 'Submission rejected. Cooldown has been reset - user can submit a new post immediately.',
                'cooldown_reset': True
            }

        except Exception as e:
            logger.error(f"❌ Rejection error: {e}")
            return {'success': False, 'error': str(e)}

    async def get_task_stats(self, wallet_address: str) -> Dict[str, Any]:
        """Get user's Twitter task statistics"""
        try:
            if not self.supabase:
                return {
                    'total_earned': 0,
                    'total_claims': 0,
                    'can_claim_today': True
                }

            claims = _wallet_filter(
                self.supabase.table('twitter_task_log')\
                    .select('reward_amount'),
                wallet_address,
            ).execute()

            total_earned = sum(float(c.get('reward_amount', 0)) for c in claims.data or [])
            total_claims = len(claims.data or [])

            eligibility = await self.check_eligibility(wallet_address)

            return {
                'total_earned': total_earned,
                'total_claims': total_claims,
                'can_claim_today': eligibility.get('can_claim', False),
                'next_claim_time': eligibility.get('next_claim_time'),
                'reward_amount': self.get_task_reward()
            }

        except Exception as e:
            logger.error(f"❌ Error getting Twitter task stats: {e}")
            return {
                'total_earned': 0,
                'total_claims': 0,
                'can_claim_today': True
            }

    def get_transaction_history(self, wallet_address: str, limit: int = 50) -> Dict[str, Any]:
        """Get user's Twitter task transaction history"""
        try:
            if not self.supabase:
                return {
                    'success': True,
                    'transactions': [],
                    'total_count': 0,
                    'total_earned': 0
                }

            logger.info(f"📋 Getting Twitter task history for {wallet_address[:8]}... (limit: {limit})")

            history = _wallet_filter(
                self.supabase.table('twitter_task_log')\
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
                        'twitter_url': record.get('twitter_url'),
                        'status': record.get('status', 'completed'),
                        'created_at': record.get('created_at'),
                        'explorer_url': f"https://explorer.celo.org/mainnet/tx/{record.get('transaction_hash')}" if record.get('transaction_hash') else None,
                        'rejection_reason': record.get('rejection_reason')
                    })

            logger.info(f"✅ Retrieved {len(transactions)} Twitter task transactions for {wallet_address[:8]}... (Total: {total_earned} G$)")

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
            logger.error(f"❌ Error getting Twitter task transaction history: {e}")
            return {
                'success': False,
                'error': str(e),
                'transactions': [],
                'total_count': 0,
                'total_earned': 0
            }

# Global instance
twitter_task_service = TwitterTaskService()

def init_twitter_task(app):
    """Initialize Twitter Task system with Flask app"""
    try:
        logger.info("🐦 Initializing Twitter Task system...")

        from flask import session, request, jsonify

        @app.route('/api/twitter-task/status', methods=['GET'])
        def get_twitter_task_status():
            """Get Twitter task status for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    stats = loop.run_until_complete(
                        twitter_task_service.get_task_stats(wallet_address)
                    )
                finally:
                    loop.close()

                return jsonify(stats), 200

            except Exception as e:
                logger.error(f"❌ Twitter task status error: {e}")
                return jsonify({'error': 'Failed to get task status'}), 500

        @app.route('/api/twitter-task/custom-message', methods=['GET'])
        def get_twitter_custom_message():
            """Get custom message for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                custom_message = twitter_task_service.get_custom_message_for_user(wallet_address)

                return jsonify({
                    'success': True,
                    'custom_message': custom_message
                })

            except Exception as e:
                logger.error(f"❌ Error getting custom message: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @app.route('/api/twitter-task/claim', methods=['POST'])
        def claim_twitter_task():
            """Claim Twitter task reward"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                data = request.get_json()
                twitter_url = data.get('twitter_url', '').strip()

                if not twitter_url:
                    return jsonify({
                        'success': False,
                        'error': 'Twitter post URL is required'
                    }), 400

                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    result = loop.run_until_complete(
                        twitter_task_service.claim_task_reward(wallet_address, twitter_url)
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
                logger.error(f"❌ Twitter task claim error: {e}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")
                return jsonify({'error': 'Failed to claim task', 'details': str(e)}), 500

        @app.route('/api/twitter-task/history', methods=['GET'])
        def get_twitter_task_history():
            """Get Twitter task transaction history for current user"""
            try:
                wallet_address = session.get('wallet_address') or session.get('wallet')
                if not wallet_address or not session.get('verified'):
                    return jsonify({'error': 'Not authenticated'}), 401

                limit = int(request.args.get('limit', 50))

                history = twitter_task_service.get_transaction_history(wallet_address, limit)

                return jsonify(history), 200

            except Exception as e:
                logger.error(f"❌ Twitter task history error: {e}")
                return jsonify({
                    'success': False,
                    'error': 'Failed to get transaction history',
                    'transactions': [],
                    'total_count': 0
                }), 500

        logger.info("✅ Twitter Task system initialized successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to initialize Twitter Task system: {e}")
        return False
