"""Shared human (face) verification gate for feature pages.

Every login_method (local, injected, walletconnect, privy, minipay) must be
face-verified on the GoodDollar Identity contract before entering
earning/spending features (dashboard, learn & earn, play & earn, swap,
savings, reloadly). Verification state comes from the on-chain
``Identity.isWhitelisted`` check in ``blockchain.is_identity_verified``,
which is cached for 5 minutes — a user who just finished face verification
gets in on the next page load without re-login.
"""
import logging

from flask import redirect

logger = logging.getLogger(__name__)

# The wallet page is the one place an unverified user CAN go — it hosts the
# face-verification entry point (Claim G$ → GoodDollar FV flow).
# `fv_required=1` tells it to explain why the user was bounced back.
FV_REQUIRED_URL = "/wallet?fv_required=1"


def wallet_is_face_verified(wallet) -> bool | None:
    """Tri-state on-chain FV read for JSON/API gates.

    ``True``/``False`` are the chain's answer; ``None`` means the chain could
    not be asked (RPC down). Callers that move money must fail closed on
    ``None`` — see ``fv_required_response``.
    """
    if not wallet:
        return False
    try:
        from blockchain import is_identity_verified
        result = is_identity_verified(wallet) or {}
        if result.get("verified") is True:
            return True
        return None if result.get("error") else False
    except Exception as exc:  # noqa: BLE001
        logger.warning("FV gate check failed for %s…: %s", str(wallet)[:10], exc)
        return None


def fv_required_response():
    """The shared 403 body for an FV-gated API. Fail-closed: an unreadable
    chain answer blocks the action rather than letting it through."""
    from flask import jsonify

    return jsonify({
        "success": False,
        "error": "Verify your identity to continue. Face verification is required for this action.",
        "code": "verified_wallet_required",
    }), 403


def human_verification_redirect(wallet):
    """Return a redirect response when `wallet` is NOT face-verified
    on-chain, otherwise None. Callers must authenticate the session first
    (this only checks FV, not login). Fails closed: an FV check error blocks
    access, matching the safety intent of the gate."""
    if wallet_is_face_verified(wallet) is True:
        return None
    logger.info(f"🪪 FV gate: blocked unverified wallet {str(wallet)[:10]}…")
    return redirect(FV_REQUIRED_URL)
