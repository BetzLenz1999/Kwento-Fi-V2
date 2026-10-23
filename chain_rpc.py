"""Bounded, cached web3 connection helpers for serverless cold starts.

Every blockchain-backed service in this repo used to build its Web3 provider and
call ``is_connected()`` in its **constructor**, and those services are
instantiated at **module import** time (see the ``*_service = ...()`` lines in
``main.py``'s import graph). On a serverless platform that means a cold start
performs a handful of *blocking* JSON-RPC round-trips before Flask can serve
anything:

    import main   ->  8+ is_connected() calls  ->  30s timeout each

Measured locally with a slow RPC, ``import main`` took 13s at 2s latency, 25s at
4s and 49s at 8s — one call after another, unbounded. Vercel's default function
limit is 10s (Hobby), so the function was killed mid-import. That is why the
symptom was a 500 / hung request with **no application log line**: Flask's
logger never ran, because the module never finished importing.

The fix has three rules:

1. **Never touch the network at import time.** ``is_connected()`` is a network
   call. Do it lazily, on first actual use.
2. **Always bound the call.** A provider without ``request_kwargs`` inherits
   ``requests``' "no timeout" default, which is how a throttled RPC turns into a
   cold-start timeout instead of a fast failure.
3. **Never probe the same chain more than once per process.** The probe result
   is cached, so eight services do not each pay for it.

``RPC_CONNECT_TIMEOUT``/``RPC_READ_TIMEOUT`` are env-overridable so a deployment
can tighten them further without a code change.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    """Read a positive float from the environment via the repo's env helper.

    ``env_utils`` already tolerates malformed/quoted/placeholder values, which is
    exactly the failure mode a serverless dashboard env var produces.
    """
    from env_utils import get_env_float

    value = get_env_float(name, default)
    return value if value > 0 else default


#: Seconds allowed for the TCP/TLS connection to an RPC endpoint.
RPC_CONNECT_TIMEOUT = _env_float("RPC_CONNECT_TIMEOUT", 3.0)
#: Seconds allowed for the response body of an RPC call.
RPC_READ_TIMEOUT = _env_float("RPC_READ_TIMEOUT", 8.0)

#: Set ``RPC_LAZY_CONNECT=0`` to restore the old eager probe (not recommended).
LAZY_CONNECT = (os.getenv("RPC_LAZY_CONNECT", "1") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# Probe results keyed by URL, so N services pointing at one chain pay once.
_probe_cache: "dict[str, bool]" = {}
_probe_lock = threading.Lock()


def request_kwargs(timeout: "float | None" = None) -> dict:
    """``request_kwargs`` for ``Web3.HTTPProvider`` with a hard timeout.

    ``web3`` forwards these to the underlying ``requests`` session, which is the
    only way to bound a JSON-RPC call. Without it a blackholed route hangs until
    the OS gives up (minutes), which on a serverless platform is a cold-start
    timeout rather than an error.
    """
    read = float(timeout) if timeout else RPC_READ_TIMEOUT
    return {
        "timeout": (RPC_CONNECT_TIMEOUT, read),
        "allow_redirects": False,
    }


def make_provider(url: str, timeout: "float | None" = None):
    """Build an ``HTTPProvider`` that can never hang.

    Kept separate from :func:`connect` so callers that only need the provider
    (no probe) still get the timeout.
    """
    from web3 import Web3

    return Web3.HTTPProvider(url, request_kwargs=request_kwargs(timeout))


def is_reachable(w3) -> bool:
    """Probe a chain, at most once per process per endpoint.

    This is the function that must never be called at import time — call it from
    the code path that actually needs the chain. A repeated probe of the same
    URL is served from cache, so a request burst does not re-probe.

    Never raises: an unreachable chain returns ``False``.
    """
    try:
        url = ""
        provider = getattr(w3, "provider", None)
        for attr in ("endpoint_uri", "_endpoint_uri", "endpoint"):
            url = getattr(provider, attr, "") or ""
            if url:
                break
        if not url:
            url = f"id:{id(provider)}"

        with _probe_lock:
            cached = _probe_cache.get(url)
        if cached is not None:
            return cached

        try:
            reachable = bool(w3.is_connected())
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            logger.warning("RPC probe failed for %s: %s", url, exc)
            reachable = False

        with _probe_lock:
            _probe_cache[url] = reachable
        return reachable
    except Exception:  # noqa: BLE001 - defensive: never break the caller
        return False


def connect(url: str, timeout: "float | None" = None):
    """Return ``(w3, reachable)`` with a bounded provider.

    ``reachable`` is ``None`` when the lazy mode is on: the caller should not
    treat "not probed yet" as "down", it should just try the call and handle the
    error. That is what keeps a cold start off the network entirely.
    """
    from web3 import Web3

    w3 = Web3(make_provider(url, timeout=timeout))
    if LAZY_CONNECT:
        return w3, None
    return w3, is_reachable(w3)


def reset_probe_cache() -> None:
    """Clear cached probe results (tests, or after a config change)."""
    with _probe_lock:
        _probe_cache.clear()
