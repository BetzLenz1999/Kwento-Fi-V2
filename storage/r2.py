"""Cloudflare R2 object storage — dependency-free S3 SigV4 client.

R2 speaks the S3 API, so presigning is just HMAC-SHA256 over a canonical
request. Everything here uses the standard library only: no ``boto3`` /
``botocore`` (which would pull in ``urllib3`` pins that fight the existing
``requests``/``httpx``/``brotlicffi`` set), and no import of Flask, so the
module imports and is testable with no packages installed.

Flow used by the Community Feed:

    presign_put()  -> browser PUTs the bytes straight to R2 (no Flask hop)
    object_head()  -> the server verifies the object exists + its real size
    delete_object()-> hard delete when a post is removed / reported

Signing details that matter:
- The payload hash is ``UNSIGNED-PAYLOAD``: a presigned PUT cannot know the
  body in advance, and R2 accepts the literal unsigned marker. Signing a real
  hash here would make every upload fail with SignatureDoesNotMatch.
- ``Content-Type`` is signed, so the browser MUST send the exact header we
  returned or the signature will not match.
- ``host`` is signed, so the endpoint must not be rewritten by a proxy.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import os
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger(__name__)

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
DEFAULT_TTL = 600
MIN_TTL = 60
MAX_TTL = 3600

# Magic-byte allow-list. A presigned PUT can only constrain Content-Type, which
# the client chooses freely, so we re-check the real bytes after upload.
IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
VIDEO_MAGIC = (
    (b"\x1a\x45\xdf\xa3", "video/webm"),
)
# ISO base media container: bytes 4..8 are "ftyp". The brand at 8..12 tells us
# whether it is a real MP4 (isom/mp42/avc1...) or a QuickTime .mov (qt  ).
ISOBMFF_BRANDS_MP4 = (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"dash", b"mmp4")
ISOBMFF_BRANDS_MOV = (b"qt  ", b"moov")


def _is_webp(head: bytes) -> bool:
    """RIFF container whose form type is WEBP.

    WEBP cannot be a prefix match: the RIFF header carries a length at 4..8
    before the ``WEBP`` form type, so ``startswith`` never works. The form-type
    check also keeps RIFF/AVI and RIFF/WAV out of the image allow-list.
    """
    return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"


# ── Configuration ────────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def account_id() -> str:
    return _env("R2_ACCOUNT_ID")


def access_key_id() -> str:
    return _env("R2_ACCESS_KEY_ID")


def secret_access_key() -> str:
    return _env("R2_SECRET_ACCESS_KEY")


def bucket() -> str:
    return _env("R2_BUCKET")


def endpoint() -> str:
    """S3 endpoint. Derived from the account id when not set explicitly."""
    explicit = _env("R2_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    acct = account_id()
    return f"https://{acct}.r2.cloudflarestorage.com" if acct else ""


def public_base_url() -> str:
    """Public read base (custom domain preferred, r2.dev as a fallback)."""
    return _env("R2_PUBLIC_BASE_URL").rstrip("/")


def credentials_configured() -> bool:
    """Whether the server can access the private S3 API, independent of CDN."""
    return bool(access_key_id() and secret_access_key() and bucket() and endpoint())


def presign_ttl() -> int:
    """Signed-URL lifetime in seconds, clamped to a sane window."""
    try:
        ttl = int(_env("R2_PRESIGN_TTL_SEC", str(DEFAULT_TTL)) or DEFAULT_TTL)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    return max(MIN_TTL, min(MAX_TTL, ttl))


def is_configured() -> bool:
    """True only when a presign can actually be produced.

    The bucket + endpoint are required for the URL; the public base is required
    for the feed to render media at all, so a half-configured deployment reads
    as "not configured" instead of handing out URLs that 404.
    """
    return credentials_configured() and bool(public_base_url())


# ── Keys ─────────────────────────────────────────────────────────────────────

_SAFE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"}


def build_key(owner_id: str, ext: str, prefix: str = "u") -> str:
    """Server-generated object key. Never accepts a client-supplied path.

    ``owner_id`` is a hash of the wallet (not the wallet itself) so the object
    path leaks no address, and a user can never overwrite another user's file:
    the random uuid component makes every key unique.
    """
    ext = (ext or "").lower().lstrip(".")
    if ext not in _SAFE_EXT:
        ext = "bin"
    owner = hashlib.sha256((owner_id or "anon").encode("utf-8")).hexdigest()[:16]
    now = _dt.datetime.now(_dt.timezone.utc)
    return f"{prefix}/{owner}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{ext}"


def is_managed_key(key: str | None) -> bool:
    """Reject anything that is not one of our own generated keys."""
    key = (key or "").strip()
    if not key or len(key) > 512 or key.startswith("/"):
        return False
    if ".." in key or "//" in key:
        return False
    return all(part not in ("", ".", "..") for part in key.split("/"))


def public_url(key: str) -> str:
    base = public_base_url()
    if not base or not key:
        return ""
    quoted = urllib.parse.quote(key.lstrip("/"), safe="/")
    return f"{base}/{quoted}"


# ── SigV4 ────────────────────────────────────────────────────────────────────

def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str,
                 service: str = SERVICE) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _uri_encode(value: str, encode_slash: bool = True) -> str:
    safe = "-_.~" if encode_slash else "-_.~/"
    return urllib.parse.quote(value, safe=safe)


def _object_path(key: str) -> str:
    """Canonical object path. Slashes stay literal.

    SigV4 requires the URI path to be encoded per segment with the separators
    left alone: encoding the bucket/key slash as ``%2F`` produces a valid-looking
    URL that R2 rejects with SignatureDoesNotMatch.
    """
    return "/" + _uri_encode(f"{bucket()}/{key.lstrip('/')}", encode_slash=False)


def _canonical_request(method: str, canonical_uri: str, canonical_query: str,
                       canonical_headers: str, signed_header_names: str,
                       payload_hash: str) -> str:
    """The AWS SigV4 canonical request (pure — unit-testable in isolation)."""
    return "\n".join([
        method.upper(),
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_header_names,
        payload_hash,
    ])


def _string_to_sign(amz_date: str, date_stamp: str, region: str,
                    service: str, canonical_request: str) -> str:
    """The AWS SigV4 string-to-sign (pure)."""
    return "\n".join([
        ALGORITHM,
        amz_date,
        f"{date_stamp}/{region}/{service}/aws4_request",
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])


def presign(method: str, key: str, *, content_type: str | None = None,
            max_bytes: int | None = None, ttl: int | None = None,
            now: _dt.datetime | None = None) -> dict:
    """Build a presigned S3 URL for ``method`` on ``key``.

    Returns ``{url, method, headers, key, public_url, expires_in}`` or an empty
    dict when the deployment is not configured. Never raises.
    """
    # Private DM attachments only need the S3-compatible credentials and
    # endpoint; they deliberately do not require R2_PUBLIC_BASE_URL because
    # they are delivered through short-lived signed GET URLs.
    if not credentials_configured():
        logger.error("r2: presign requested but credentials are not configured")
        return {}
    if not is_managed_key(key):
        logger.error("r2: refusing to presign a non-managed key")
        return {}

    method = method.upper()
    ttl = max(MIN_TTL, min(MAX_TTL, int(ttl or presign_ttl())))
    host = endpoint().split("://", 1)[-1].strip("/")
    scheme = "https" if endpoint().startswith("https") else "http"

    stamp = (now or _dt.datetime.now(_dt.timezone.utc))
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = stamp.strftime("%Y%m%d")

    # Content-Type is signed, so the browser must send back the exact value we
    # returned; a mismatch is a SignatureDoesNotMatch, which is the point.
    signed_header_names = "content-type;host" if content_type else "host"
    canonical_headers = (
        f"content-type:{content_type}\nhost:{host}\n" if content_type
        else f"host:{host}\n"
    )

    # Query parameters are signed, so they must be sorted by name.
    params: list[tuple[str, str]] = [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential",
         f"{access_key_id()}/{date_stamp}/auto/{SERVICE}/aws4_request"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(ttl)),
        ("X-Amz-SignedHeaders", signed_header_names),
    ]
    canonical_uri = _object_path(key)
    canonical_query = "&".join(
        f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(params)
    )

    canonical_request = _canonical_request(
        method, canonical_uri, canonical_query, canonical_headers,
        signed_header_names, UNSIGNED_PAYLOAD,
    )
    string_to_sign = _string_to_sign(
        amz_date, date_stamp, "auto", SERVICE, canonical_request,
    )

    signature = hmac.new(
        _signing_key(secret_access_key(), date_stamp, "auto"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    url = (
        f"{scheme}://{host}{canonical_uri}"
        f"?{canonical_query}&X-Amz-Signature={signature}"
    )

    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    return {
        "url": url,
        "method": method,
        "headers": headers,
        "key": key,
        "public_url": public_url(key),
        "expires_in": ttl,
    }


def presign_put(key: str, content_type: str, max_bytes: int | None = None,
                ttl: int | None = None) -> dict:
    """Presigned PUT for a browser upload."""
    return presign("PUT", key, content_type=content_type, max_bytes=max_bytes, ttl=ttl)


def presign_get(key: str, ttl: int | None = None) -> str:
    """Presigned GET for a private object (not needed while the bucket is public)."""
    signed = presign("GET", key, ttl=ttl)
    return signed.get("url", "")


# ── Object operations ────────────────────────────────────────────────────────

def _signed_headers(method: str, key: str, *, extra: dict | None = None,
                    now: _dt.datetime | None = None) -> dict:
    """Headers for a server-side (non-presigned) request, signed with a real
    payload hash (an empty body for HEAD/DELETE)."""
    host = endpoint().split("://", 1)[-1].strip("/")
    stamp = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = stamp.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_uri = _object_path(key)
    headers = {"host": host, "x-amz-content-sha256": payload_hash,
               "x-amz-date": amz_date}
    if extra:
        headers.update({k.lower(): v for k, v in extra.items()})

    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join([
        method, canonical_uri, "", canonical_headers, signed_names, payload_hash,
    ])
    string_to_sign = "\n".join([
        ALGORITHM, amz_date, f"{date_stamp}/auto/{SERVICE}/aws4_request",
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_access_key(), date_stamp, "auto"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    out = dict(headers)
    out["Authorization"] = (
        f"{ALGORITHM} Credential={access_key_id()}/{date_stamp}/auto/{SERVICE}/aws4_request, "
        f"SignedHeaders={signed_names}, Signature={signature}"
    )
    return out


def _object_url(key: str) -> str:
    """Absolute URL of one object on the S3 endpoint."""
    host = endpoint().split("://", 1)[-1].strip("/")
    scheme = "https" if endpoint().startswith("https") else "http"
    return f"{scheme}://{host}{_object_path(key)}"


def _request(method: str, key: str, *, extra: dict | None = None,
             timeout: int = 15):
    """Signed server-side request. Returns a response or None (never raises)."""
    if not credentials_configured() or not is_managed_key(key):
        return None
    try:
        headers = _signed_headers(method, key, extra=extra)
        req = urllib.request.Request(_object_url(key), method=method,
                                     headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
    except Exception as exc:  # noqa: BLE001
        logger.warning("r2: %s %s failed: %s", method, key, exc)
        return None


def object_head(key: str) -> dict | None:
    """Metadata for an uploaded object, or None when it does not exist.

    This is the trust boundary: the browser uploads directly to R2, so the
    server must confirm the object is really there (and how big it is) before
    any post row references it.
    """
    response = _request("HEAD", key)
    if response is None:
        return None
    try:
        with response:
            size = response.headers.get("Content-Length")
            return {
                "exists": True,
                "bytes": int(size) if size is not None else None,
                "content_type": response.headers.get("Content-Type"),
                "etag": (response.headers.get("ETag") or "").strip('"'),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("r2: HEAD read failed for %s: %s", key, exc)
        return None


def object_get(key: str, *, max_bytes: int) -> dict | None:
    """Read one managed object, refusing bodies larger than ``max_bytes``.

    This server-side path is used for the deliberately public profile-avatar
    endpoint; it does not depend on the bucket's public CDN being enabled.
    """
    try:
        limit = max(1, int(max_bytes))
    except (TypeError, ValueError):
        return None
    response = _request("GET", key, timeout=20)
    if response is None:
        return None
    try:
        with response:
            raw_size = response.headers.get("Content-Length")
            if raw_size is not None:
                try:
                    if int(raw_size) > limit:
                        return None
                except (TypeError, ValueError):
                    pass
            body = response.read(limit + 1) or b""
            if len(body) > limit:
                return None
            return {
                "body": body,
                "bytes": len(body),
                "content_type": response.headers.get("Content-Type"),
                "etag": (response.headers.get("ETag") or "").strip('"'),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("r2: GET read failed for %s: %s", key, exc)
        return None


def delete_object(key: str) -> bool:
    """Hard-delete one object. Idempotent — a missing key still reports True
    only when R2 accepted the request."""
    response = _request("DELETE", key)
    if response is None:
        return False
    try:
        response.close()
    except Exception:  # noqa: BLE001
        pass
    return True


def delete_objects(keys: list[str]) -> int:
    """Best-effort batch delete. Returns how many R2 accepted.

    ``is_managed_key`` is re-checked here rather than relying on the guard
    inside ``_request``: a future refactor of the transport must not be able to
    turn this into an arbitrary-key delete.
    """
    removed = 0
    for key in keys or []:
        if key and is_managed_key(key) and delete_object(key):
            removed += 1
    return removed


def read_prefix(key: str, length: int = 32) -> bytes:
    """First ``length`` bytes of an object (magic-byte sniffing)."""
    if not is_configured() or not is_managed_key(key):
        return b""
    try:
        headers = _signed_headers("GET", key, extra={"Range": f"bytes=0-{length - 1}"})
        req = urllib.request.Request(_object_url(key), method="GET",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
            return response.read(length) or b""
    except Exception as exc:  # noqa: BLE001
        logger.warning("r2: range read failed for %s: %s", key, exc)
        return b""


# ── Media sniffing ───────────────────────────────────────────────────────────

def sniff_media_type(head: bytes) -> str | None:
    """Return ``image`` / ``video`` / ``None`` from the file's magic bytes.

    The declared MIME type is client-controlled; this is the check that
    actually decides what the bytes are.
    """
    head = head or b""
    if not head:
        return None
    for magic, _mime in IMAGE_MAGIC:
        if head.startswith(magic):
            return "image"
    for magic, _mime in VIDEO_MAGIC:
        if head.startswith(magic):
            return "video"
    if _is_webp(head):
        return "image"
    # ISO base media file format (MP4 / MOV): "....ftyp<brand>"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in ISOBMFF_BRANDS_MOV:
            return "video"
        if brand in ISOBMFF_BRANDS_MP4:
            return "video"
        # Unknown brand but a valid ftyp box: still a media container.
        return "video"
    return None


def sniff_mime(head: bytes) -> str | None:
    """Best-effort MIME from magic bytes (used to catch a lying extension)."""
    head = head or b""
    for magic, mime in IMAGE_MAGIC:
        if head.startswith(magic):
            return mime
    for magic, mime in VIDEO_MAGIC:
        if head.startswith(magic):
            return mime
    if _is_webp(head):
        return "image/webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        if head[8:12] in ISOBMFF_BRANDS_MOV:
            return "video/quicktime"
        return "video/mp4"
    return None
