#!/usr/bin/env python3
"""Invoke the protected weekly leaderboard snapshot worker.

Schedule this script from the application's existing cron/runtime. It refreshes
the current UTC-week provisional snapshot; it does not finalize winners or pay
rewards. Configure WEEKLY_EXP_LEADERBOARD_WORKER_URL and
WEEKLY_EXP_LEADERBOARD_CRON_TOKEN in the scheduler's secret environment.
"""
from __future__ import annotations

import json
import os
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT_PATH = "/community/api/cron/weekly-leaderboard-refresh"
MAX_ATTEMPTS = 3
RETRY_DELAYS = (5, 15)
TIMEOUT_SECONDS = 900


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_endpoint(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if (parts.path != ENDPOINT_PATH or parts.query or parts.fragment
            or parts.username or parts.password):
        raise ValueError("worker URL must be the exact leaderboard refresh endpoint")
    loopback = (parts.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError("worker URL must use HTTPS (HTTP is allowed only on loopback)")
    if not parts.netloc or not parts.hostname:
        raise ValueError("worker URL has no valid host")
    return parts.geturl()


def _safe_result(data) -> dict:
    if not isinstance(data, dict):
        return {"success": False, "status": "invalid_response"}
    allowed = ("success", "status", "week_start", "generated_at",
               "participant_count", "public_count", "garden_status")
    return {key: data[key] for key in allowed if key in data}


def invoke_worker(url: str, token: str, opener=None, sleeper=None) -> tuple[int, dict]:
    """Call once with bounded retry on network, rate-limit, or server failures."""
    endpoint = validate_endpoint(url)
    token = (token or "").strip()
    if len(token) < 32:
        raise ValueError("worker token must contain at least 32 characters")
    open_url = opener or build_opener(_NoRedirect()).open
    sleep = sleeper or time.sleep
    request = Request(endpoint, data=b"{}", method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    for attempt in range(MAX_ATTEMPTS):
        status_code = 0
        body = b""
        try:
            with open_url(request, timeout=TIMEOUT_SECONDS) as response:
                status_code = int(response.getcode())
                body = response.read(64 * 1024)
        except HTTPError as exc:
            status_code = int(exc.code)
            try:
                body = exc.read(64 * 1024)
            except Exception:  # noqa: BLE001
                body = b""
        except (URLError, TimeoutError, socket.timeout, OSError):
            if attempt + 1 < MAX_ATTEMPTS:
                sleep(RETRY_DELAYS[attempt])
                continue
            return 1, {"success": False, "status": "network_error"}

        retryable = status_code == 429 or status_code >= 500
        if retryable and attempt + 1 < MAX_ATTEMPTS:
            sleep(RETRY_DELAYS[attempt])
            continue
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, ValueError):
            data = {}
        result = _safe_result(data)
        if 200 <= status_code < 300 and result.get("success") is True:
            return 0, result
        if not result:
            result = {"success": False, "status": "http_error"}
        result["http_status"] = status_code
        return 1, result

    return 1, {"success": False, "status": "retry_exhausted"}


def main() -> int:
    url = os.getenv("WEEKLY_EXP_LEADERBOARD_WORKER_URL", "")
    token = os.getenv("WEEKLY_EXP_LEADERBOARD_CRON_TOKEN", "")
    if not url or not token:
        print(json.dumps({"success": False, "status": "configuration_error",
                          "message": "Worker URL and token must be configured in the scheduler environment."}))
        return 2
    try:
        code, result = invoke_worker(url, token)
    except ValueError as exc:
        print(json.dumps({"success": False, "status": "configuration_error",
                          "message": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
