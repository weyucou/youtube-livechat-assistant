r"""jawed (youtube-livechat-assistant) deployed-environment smoke test.

Runs the automatable checks against a live jawed API Gateway deployment (the
invoke URL `zappa deploy` prints, including the stage path).

Usage:
    python validation/smoke_test.py \\
        --api-url https://abc123.execute-api.ap-northeast-1.amazonaws.com/dev

Exits 0 only when every automated check passes.

Automated checks:
    Infrastructure:
        1 API hostname resolves via DNS
        2 TLS certificate is valid, trusted, and issued for the host
    Public API:
        3 GET /health returns 200 with {"status": "healthy"}
    Auth boundary:
        4 GET /channels/ while anonymous returns 401 (not 500)
    Persistence (exposes the SQLite-on-Lambda blocker, #17):
        5 GET /channels/<unknown>/accepting-requests returns 404 (not 500) —
          a 500 here means the master DB is not readable on Lambda

The authenticated admin flows (register channel, configure OAuth, submit
requests) need a JWT and real channel data and are verified by hand.
"""

# ruff: noqa: T201, S310  # validation script: prints a report, opens the operator-supplied API URL

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

if TYPE_CHECKING:
    from http.client import HTTPMessage

REQUEST_TIMEOUT_SECONDS = 15
TLS_EXPIRY_WARNING_DAYS = 30
UNKNOWN_CHANNEL_ID = "smoke-test-nonexistent-channel"

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR = 500


def _record(results: list, label: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    results.append((label, passed, detail))


def _get(
    url: str, method: str = "GET", headers: dict | None = None
) -> tuple[int, HTTPMessage | None, bytes]:
    """Return (status, headers, body), surfacing errors as a status instead of raising."""
    req = Request(url, method=method, headers=headers or {})
    try:
        resp = build_opener().open(req, timeout=REQUEST_TIMEOUT_SECONDS)
        return resp.status, resp.headers, resp.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read() if exc.fp else b""
    except URLError as exc:
        print(f"    (request error for {url}: {exc.reason})")
        return 0, None, b""


def check_dns(results: list, url: str) -> None:
    host = urlparse(url).hostname
    if not host:
        _record(results, "API hostname resolves", False, f"no hostname in {url}")
        return
    try:
        addrs = socket.getaddrinfo(host, None)
        addr = addrs[0][4][0] if addrs else "<none>"
        _record(results, "API hostname resolves", True, f"{host} -> {addr}")
    except socket.gaierror as exc:
        _record(results, "API hostname resolves", False, f"{host}: {exc}")


def check_tls(results: list, url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        _record(results, "TLS certificate valid for host", False, f"no hostname in {url}")
        return
    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=REQUEST_TIMEOUT_SECONDS) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
    except (TimeoutError, ssl.SSLError, socket.gaierror, OSError) as exc:
        _record(results, "TLS certificate valid for host", False, f"{host}: {exc}")
        return
    not_after = cert.get("notAfter") if cert else None
    expires_at = (
        datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        if not_after
        else None
    )
    days_left = (expires_at - datetime.now(tz=UTC)).days if expires_at else -1
    passed = days_left > TLS_EXPIRY_WARNING_DAYS
    _record(results, "TLS certificate valid for host", passed, f"expires_in_days={days_left}")


def check_health(results: list, base: str) -> None:
    status, _, body = _get(base + "/health")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        payload = {}
    ok = status == HTTP_OK and payload.get("status") == "healthy"
    _record(results, "GET /health returns 200 {status: healthy}", ok, f"status={status}, body={payload}")


def check_channels_requires_auth(results: list, base: str) -> None:
    status, _, _ = _get(base + "/channels/")
    _record(
        results,
        "GET /channels/ anonymous -> 401 (not 500)",
        status == HTTP_UNAUTHORIZED,
        f"status={status}",
    )


def check_accepting_requests_db_read(results: list, base: str) -> None:
    status, _, _ = _get(base + f"/channels/{UNKNOWN_CHANNEL_ID}/accepting-requests")
    # 404 = master DB read worked and the channel is absent (expected).
    # 500 = the master DB is not readable on Lambda (the SQLite blocker, #17).
    passed = status == HTTP_NOT_FOUND
    hint = " — DB not readable on Lambda? see #17" if status == HTTP_SERVER_ERROR else ""
    _record(
        results,
        "GET /channels/<unknown>/accepting-requests -> 404 (DB read OK)",
        passed,
        f"status={status}{hint}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="jawed deployed-environment smoke test")
    parser.add_argument(
        "--api-url",
        required=True,
        help="Deployed API base URL incl. stage path (https://...execute-api.../dev)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON results after the report")
    args = parser.parse_args()

    base = args.api_url.rstrip("/")
    print(f"jawed smoke test — api={base}")
    print("=" * 70)

    results: list[tuple[str, bool, str]] = []

    print("\n--- Infrastructure ---")
    check_dns(results, base)
    check_tls(results, base)

    print("\n--- Public API ---")
    check_health(results, base)

    print("\n--- Auth boundary ---")
    check_channels_requires_auth(results, base)

    print("\n--- Persistence (SQLite-on-Lambda, #17) ---")
    check_accepting_requests_db_read(results, base)

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print("\n" + "=" * 70)
    print(f"Automated results: {passed} passed, {failed} failed, {len(results)} total")
    print("\nManual checks (authenticated, need a JWT):")
    print("  - POST /auth/register + POST /auth/login return a usable JWT")
    print("  - POST /channels/ (admin) registers a channel; GET /channels/<id> reads it back")
    print("  - GET /channels/<id>/accepting-requests reflects the configured window")

    if args.json:
        print()
        print(
            json.dumps(
                {
                    "api_url": base,
                    "results": [
                        {"label": label, "passed": ok, "detail": detail}
                        for label, ok, detail in results
                    ],
                    "passed": passed,
                    "failed": failed,
                }
            )
        )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
