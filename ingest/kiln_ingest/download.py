"""Granule downloads from the LANCE and LP DAAC distribution hosts.

Redirects are followed by hand rather than by ``requests``, because the two
feeds bounce differently and the bearer token must not follow blindly.

A LANCE download stays on one host. An archive download does not: with a token,
``data.lpdaac.earthdatacloud.nasa.gov`` answers 303 to a pre-signed CloudFront
URL, and without one it answers 302 to ``urs.earthdata.nasa.gov``. So the token
has to survive a NASA-to-NASA hop that ``requests`` would strip, and must not
survive the hop to CloudFront -- which does not want it anyway, since its
authorisation is the signature in its own query string.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from .cmr import GranuleRef

LOG = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
CHUNK_BYTES = 1024 * 1024

# Where the Earthdata bearer token may be sent. Suffix matching is anchored on
# a leading dot so that a lookalike host such as "not-nasa.gov" cannot match.
EARTHDATA_HOST = "nasa.gov"
EARTHDATA_HOST_SUFFIX = ".nasa.gov"

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 10


class DownloadError(RuntimeError):
    """A granule could not be fetched after every retry."""


def earthdata_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def bearer_allowed(url: str) -> bool:
    """Whether the Earthdata token may be sent to this URL.

    Two conditions, both required: the host is nasa.gov or a subdomain of it,
    and the scheme is HTTPS. A redirect is attacker-influenced input in the
    general case, and a credential that follows one anywhere is a credential
    that leaks -- so this answers no by default and yes only for the hosts NASA
    actually authenticates against.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == EARTHDATA_HOST or host.endswith(EARTHDATA_HOST_SUFFIX)


def _stream_to_file(
    session: Any, url: str, token: str, partial: Path, timeout: int
) -> None:
    """Follow redirects by hand, re-deciding the token at every hop."""
    current = url

    for _ in range(MAX_REDIRECTS + 1):
        headers = earthdata_headers(token) if bearer_allowed(current) else {}
        with session.get(
            current, headers=headers, stream=True, timeout=timeout, allow_redirects=False
        ) as response:
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location") or response.headers.get("location")
                if not location:
                    raise DownloadError(
                        f"{response.status_code} from {current} with no Location header"
                    )
                following = urljoin(current, location)
                if bearer_allowed(current) and not bearer_allowed(following):
                    LOG.debug(
                        "dropping the Earthdata token across a redirect to %s",
                        urlparse(following).hostname,
                    )
                current = following
                continue

            response.raise_for_status()
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                    if chunk:
                        handle.write(chunk)
            return

    raise DownloadError(f"more than {MAX_REDIRECTS} redirects starting from {url}")


def download_granule(
    session: Any,
    url: str,
    destination: Path,
    token: str,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: int = 300,
    backoff_base: float = 2.0,
    sleep: Any = time.sleep,
) -> Path:
    """Stream one granule to disk, retrying with exponential backoff.

    Each attempt writes to a ``.part`` file and renames on success, so a
    truncated download can never be mistaken for a complete granule.
    """
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            _stream_to_file(session, url, token, partial, timeout)
            if partial.stat().st_size == 0:
                raise DownloadError(f"empty body from {url}")
            partial.replace(destination)
            return destination
        except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < attempts:
                delay = backoff_base**attempt
                LOG.warning(
                    "download attempt %d/%d failed for %s (%s); retrying in %.0fs",
                    attempt,
                    attempts,
                    url,
                    exc,
                    delay,
                )
                sleep(delay)

    raise DownloadError(f"failed to download {url} after {attempts} attempts: {last_error}")


# --- Direct S3 access (archive provider, in-region AWS only) ------------------------
#
# LP DAAC's cloud archive is backed by S3 in us-west-2 and will hand out
# temporary AWS credentials scoped to it, but the bucket policy only accepts
# requests that actually originate inside that region (verified 2026-09-03: a
# real credential exchange from outside AWS succeeds, and the S3 read it
# authorizes then 403s). So this is opt-in and self-disabling, never assumed:
# a worker running anywhere else asks once, fails once, logs why, and falls
# back to the ordinary HTTPS path in ``download_granule`` for the rest of its
# run rather than paying S3-attempt latency on every single granule.

S3_CREDENTIALS_URL = "https://data.lpdaac.earthdatacloud.nasa.gov/s3credentials"
S3_REGION = "us-west-2"

# Credentials are issued with a ~1 hour lifetime. Refreshed this far before
# expiry so a granule download never starts against a token about to turn
# over mid-stream.
S3_REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: datetime

    @property
    def stale(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at - S3_REFRESH_MARGIN


def parse_s3_url(s3_url: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key`` URL. Raises on anything else."""
    parsed = urlparse(s3_url)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise DownloadError(f"not a usable s3:// URL: {s3_url!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def fetch_s3_credentials(
    session: Any, token: str, url: str = S3_CREDENTIALS_URL, timeout: int = 30
) -> S3Credentials:
    """Exchange the Earthdata bearer token for temporary AWS credentials.

    The response's ``expiration`` is ``"YYYY-MM-DD HH:MM:SS+00:00"`` (space
    rather than ``T``, verified against a real response), which
    ``datetime.fromisoformat`` reads directly on Python 3.11+.
    """
    response = session.get(url, headers=earthdata_headers(token), timeout=timeout)
    response.raise_for_status()
    body = response.json()
    return S3Credentials(
        access_key_id=body["accessKeyId"],
        secret_access_key=body["secretAccessKey"],
        session_token=body["sessionToken"],
        expires_at=datetime.fromisoformat(body["expiration"]),
    )


class S3Fetcher:
    """Direct-S3 downloads for one run, with self-disable on the first failure.

    Every method here is best-effort: any error -- a 403 from outside
    us-west-2, an expired token, a missing ``boto3`` -- disables the fetcher
    for the rest of the process and lets the caller fall back to
    :func:`download_granule`. A caller is expected to pass this in
    unconditionally when ``--s3-direct`` is set; it is this class's job to be
    a safe no-op everywhere that flag does not actually help.
    """

    def __init__(
        self, session: Any, token: str, credentials_url: str = S3_CREDENTIALS_URL
    ) -> None:
        self._session = session
        self._token = token
        self._credentials_url = credentials_url
        self._credentials: S3Credentials | None = None
        self._client: Any = None
        self.disabled = False

    def _refreshed_client(self) -> Any:
        if self._credentials is None or self._credentials.stale:
            self._credentials = fetch_s3_credentials(
                self._session, self._token, self._credentials_url
            )
            import boto3  # noqa: PLC0415 - only needed when S3-direct is actually used

            self._client = boto3.client(
                "s3",
                region_name=S3_REGION,
                aws_access_key_id=self._credentials.access_key_id,
                aws_secret_access_key=self._credentials.secret_access_key,
                aws_session_token=self._credentials.session_token,
            )
        return self._client

    def download(self, s3_url: str, destination: Path) -> Path:
        """Stream one object to disk via S3, or raise on any failure.

        Same ``.part``-then-rename atomicity as :func:`download_granule`, but a
        single attempt: a caller that wants HTTPS-style retries should retry
        by falling back to :func:`download_granule` instead, not by retrying a
        request class that just proved itself unusable.
        """
        if self.disabled:
            raise DownloadError("S3-direct is disabled for this run")
        bucket, key = parse_s3_url(s3_url)
        partial = destination.with_suffix(destination.suffix + ".part")
        try:
            client = self._refreshed_client()
            client.download_file(bucket, key, str(partial))
            if partial.stat().st_size == 0:
                raise DownloadError(f"empty body from s3://{bucket}/{key}")
            partial.replace(destination)
            return destination
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def disable(self, reason: str) -> None:
        self.disabled = True
        LOG.warning(
            "S3-direct access is not usable from here (%s); falling back to HTTPS "
            "for the rest of this run. This is expected outside AWS us-west-2.",
            reason,
        )


def download_granule_auto(
    session: Any,
    ref: "GranuleRef",
    destination: Path,
    token: str,
    s3_fetcher: S3Fetcher | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> Path:
    """Prefer direct S3 when available, otherwise the ordinary HTTPS path.

    A live, not-yet-disabled fetcher gets exactly one try: on failure the
    fetcher disables itself (so the rest of the run stops paying for doomed
    S3 attempts) and this falls through to :func:`download_granule`, which
    retries with backoff as usual.
    """
    if s3_fetcher is not None and not s3_fetcher.disabled and ref.s3_url:
        try:
            return s3_fetcher.download(ref.s3_url, destination)
        except Exception as exc:  # noqa: BLE001 - any failure means "use HTTPS instead"
            s3_fetcher.disable(f"{type(exc).__name__}: {exc}")

    return download_granule(session, ref.url, destination, token, attempts=attempts)
