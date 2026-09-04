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
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

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
