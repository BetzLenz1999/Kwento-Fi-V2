"""Runtime policies for request-driven and serverless deployments."""

import os


_TRUTHY = {"1", "true", "yes", "on"}


def _is_enabled(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def is_serverless_runtime(environ=None) -> bool:
    """Return whether the process is running in Vercel or AWS Lambda."""
    env = os.environ if environ is None else environ
    return _is_enabled(env.get("VERCEL")) or bool(
        str(env.get("AWS_LAMBDA_FUNCTION_NAME") or "").strip()
    )


def should_run_inprocess_background_jobs(environ=None) -> bool:
    """Keep process-local workers off in serverless unless explicitly opted in.

    Serverless instances are request-driven and may scale horizontally, so a
    daemon thread started at module import can be duplicated across instances.
    """
    env = os.environ if environ is None else environ
    if not is_serverless_runtime(env):
        return True
    return _is_enabled(env.get("VERCEL_BACKGROUND_JOBS_ENABLED"))
