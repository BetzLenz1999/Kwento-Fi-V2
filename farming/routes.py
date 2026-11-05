import logging
import os
from flask import Blueprint, jsonify, render_template, request, session

from env_utils import get_env_int

logger = logging.getLogger(__name__)

farming_bp = Blueprint("farming", __name__, url_prefix="/farming")

FARMING_CONTRACT_ADDRESS = os.getenv("FARMING_CONTRACT_ADDRESS", "")
GD_TOKEN_ADDRESS = os.getenv(
    "GOODDOLLAR_CONTRACT_ADDRESS",
    "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A",
)
CHAIN_ID = get_env_int("CHAIN_ID", 42220)

# Reads (farm state, receipts) go through a public RPC so the page never pops a
# wallet prompt just to render, and never lags behind the phone's wallet node.
CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CELO_RPC_FALLBACKS = [
    u.strip() for u in os.getenv(
        "CELO_RPC_FALLBACKS",
        "https://forno.celo.org,https://rpc.ankr.com/celo,https://celo.drpc.org",
    ).split(",") if u.strip()
]

FARMING_CONFIG = {
    "min_farm_gd": 1000,
    "chicken_price_gd": 100,
    "min_chickens": 10,
    "eggs_per_chicken_per_day": 1,
    "farm_days": 30,
    "monthly_profit_bps": 1000,
    "monthly_profit_percent": 10,
    "min_withdraw_gd": 500,
}


def _require_auth():
    wallet = session.get("wallet") or session.get("wallet_address")
    verified = session.get("verified") or session.get("ubi_verified")
    return wallet, verified


@farming_bp.route("/")
def farming_home():
    wallet, verified = _require_auth()
    # Guests (no session wallet) may view the page: the shared "Get Started"
    # connect card renders in place of the play panel so a shared /farming link
    # still converts. Signing stays impossible without a connect (a guest has
    # an empty wallet, so no farm action can be built), so nothing is exposed.
    is_guest = not (wallet and verified)
    if is_guest:
        wallet = ""
    community_embed = request.args.get("embed") == "1"

    return render_template(
        "farming.html",
        wallet=wallet,
        is_guest=is_guest,
        login_method=session.get("login_method", ""),
        walletconnect_project_id=os.getenv("WALLETCONNECT_PROJECT_ID", ""),
        farming_contract=FARMING_CONTRACT_ADDRESS,
        gd_contract=GD_TOKEN_ADDRESS,
        chain_id=CHAIN_ID,
        celo_rpc=CELO_RPC_URL,
        celo_rpc_fallbacks=CELO_RPC_FALLBACKS,
        farming_config=FARMING_CONFIG,
        community_embed=community_embed,
    )


@farming_bp.route("/api/config")
def api_config():
    # Preserve the original V1 fields for legacy clients; add versioned metadata.
    from farming.blockchain import farming_v2_runtime
    v2 = farming_v2_runtime()
    return jsonify({
        "contract": FARMING_CONTRACT_ADDRESS,
        "gd_contract": GD_TOKEN_ADDRESS,
        "chain_id": CHAIN_ID,
        "config": FARMING_CONFIG,
        "versions": {
            "v1": {"contract": FARMING_CONTRACT_ADDRESS, "enabled": bool(FARMING_CONTRACT_ADDRESS),
                   "source": "legacy_v1_config", "config": FARMING_CONFIG},
            "v2": {"contract": v2.get("contract", ""), "enabled": v2["enabled"],
                   "verified": v2["verified"], "source": v2["source"], "reason": v2["reason"],
                   "token": v2.get("token", ""), "token_decimals": v2.get("token_decimals", 18),
                   "chain_id": v2["chain_id"], "params": v2.get("params", {
                       "duration_seconds": 604800, "min_principal_wei": 1000 * 10**18,
                       "max_principal_wei": 20000 * 10**18, "reward_bps": 500}),
                   "reward_description": "Sponsor-funded reward cap up to 5% per 7-day cycle; not guaranteed"}
        }
    })


@farming_bp.route("/api/v2/config")
def api_v2_config():
    from farming.blockchain import farming_v2_runtime
    status = farming_v2_runtime()
    # Never serialize the runtime object wholesale; only approved public fields.
    return jsonify({"version": "v2", "enabled": status["enabled"], "verified": status["verified"],
        "contract": status.get("contract", ""), "token": status.get("token", ""),
        "chain_id": status["chain_id"], "token_decimals": status.get("token_decimals", 18),
        "params": status.get("params", {"duration_seconds": 604800, "min_principal_wei": 1000*10**18,
                                          "max_principal_wei": 20000*10**18, "reward_bps": 500}),
        "source": status["source"], "reason": status["reason"],
        "reward_description": "Sponsor-funded reward cap up to 5% per 7-day cycle; not guaranteed"})


@farming_bp.route("/api/v2/history")
def api_v2_history():
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Not authenticated"}), 401
    from farming.blockchain import farming_v2_runtime, fetch_wallet_farm_v2_history, get_deploy_block
    status = farming_v2_runtime()
    rows = []
    if status["verified"]:
        try:
            deploy_block = get_env_int("FARMINGV2_DEPLOY_BLOCK", 0) or get_deploy_block(
                status["contract"], env_var_name="FARMINGV2_DEPLOY_BLOCK")
            if deploy_block > 0:
                rows = fetch_wallet_farm_v2_history(status["contract"], wallet, deploy_block=deploy_block,
                    force=str(request.args.get("force", "")).lower() in ("1", "true", "yes"))
        except Exception as exc:
            logger.warning("Farming V2 history unavailable (%s)", type(exc).__name__)
        return jsonify({"transactions": rows, "contract": status.get("contract", ""), "wallet": wallet,
        "version": "v2", "source": "onchain" if status["verified"] and rows is not None else "unavailable",
        "verified": status["verified"], "count": len(rows)})


@farming_bp.route("/api/history")
def api_history():
    """Return the CONNECTED wallet's complete farm history from the contract.

    The wallet is taken from the SESSION only — never from a query parameter or
    request body. That is what guarantees each user sees their own on-chain
    activity and nothing else: a caller cannot ask for another address, and the
    underlying log filter is anchored on the session wallet's indexed topic.

    The contract events are the durable record, so this survives cleared browser
    storage, a new device, and private mode. Returns ``[]`` (never an error) when
    the contract is not configured or the RPC is unreachable, so the page can
    fall back to its locally tracked rows.
    """
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Not authenticated"}), 401

    if not FARMING_CONTRACT_ADDRESS:
        return jsonify({"transactions": [], "contract": "", "source": "onchain"})

    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    try:
        from farming.blockchain import fetch_wallet_farm_history
        rows = fetch_wallet_farm_history(
            FARMING_CONTRACT_ADDRESS, wallet, force=force)
    except Exception as exc:
        logging.getLogger(__name__).error("farming history fetch failed: %s", exc)
        rows = []

    return jsonify({
        "transactions": rows,
        "contract": FARMING_CONTRACT_ADDRESS,
        "wallet": wallet,
        "source": "onchain",
        "count": len(rows),
    })
