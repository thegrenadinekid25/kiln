"""Granule downloads from the LP DAAC cloud distribution host.

Copied from the daily pipeline's downloader rather than imported, for the same
reason the science constants are: the two tools ship separately. The one
difference that matters here is scale -- this one runs roughly 17,700 times per
product -- so the retry budget is larger and the failure is reported rather than
fatal, since a single unreachable day must not end a 24-year sweep.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 4
CHUNK_BYTES = 1024 * 1024

# A CMG granule is 40-70 MB. Anything far below that is a truncated body or an
# error page that arrived with a 200, both of which would parse as a corrupt
# HDF file several steps later, where the cause is much harder to see.
MIN_PLAUSIBLE_BYTES = 1024 * 1024


class DownloadError(RuntimeError):
    """A granule could not be fetched after every retry."""


def earthdata_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def download_granule(
    session: Any,
    url: str,
    destination: Path,
    token: str,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: int = 600,
    backoff_base: float = 2.0,
    sleep: Any = time.sleep,
    min_bytes: int = MIN_PLAUSIBLE_BYTES,
) -> Path:
    """Stream one granule to disk, retrying with exponential backoff.

    Each attempt writes to a ``.part`` file and renames on success, so a
    truncated download can never be mistaken for a complete granule.

    The bearer token goes to the LP DAAC host, which answers with a redirect to
    a pre-signed CloudFront URL. ``requests`` drops the Authorization header on
    a cross-host redirect, which is exactly right: the signature in the redirect
    target is the credential from that point on, and forwarding an Earthdata
    token to a CDN would hand it to a host that has no business holding it.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with session.get(
                url, headers=earthdata_headers(token), stream=True, timeout=timeout
            ) as response:
                response.raise_for_status()
                with open(partial, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                        if chunk:
                            handle.write(chunk)
            size = partial.stat().st_size
            if size < min_bytes:
                raise DownloadError(f"body from {url} was only {size} bytes")
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
