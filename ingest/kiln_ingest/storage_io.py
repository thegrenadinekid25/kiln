"""Raster tile and manifest writes to Supabase Storage.

The ``kiln-tiles`` bucket is public-read; the pipeline is its only writer, using
the same service key that writes the ``kiln`` schema. Layout:

    manifest.json                 what the frontend reads first, newest day
    {date}/{z}/{x}/{y}.png        one day's pyramid, ISO date prefix
    manifest-alltime.json         the all-time view's manifest
    alltime/{z}/{x}/{y}.png       the all-time pyramid
    alltime-state/{x}/{y}.npy     base-zoom all-time state, exact centi-Celsius

Both manifests sit at the bucket root and their shapes are contracts with
``web/``: the frontend expands ``tile_url_template`` to build tile URLs, so keys
are added, never renamed or dropped. Each is written last, after every tile it
describes is up, so a reader never sees a manifest pointing at a half-uploaded
pyramid.

The dated prefixes are permanent too (decision 2026-09-03: forward-looking
rewind). Every day's raster pyramid is kept from here on, building a scrubbable
archive; ``prune_old_dates`` no longer deletes anything. **The ``alltime`` and
``alltime-state`` prefixes were already permanent** by a separate, older
mechanism: pruning selects prefixes that parse as ISO dates, which neither of
these does, and a test holds that line.

Payload construction is separated from transport, as in :mod:`supabase_io`, so
the shapes can be tested without a network or a service key.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from .raster import MAX_ZOOM, MIN_ZOOM
from .supabase_io import SUPABASE_URL

LOG = logging.getLogger(__name__)

STORAGE_BASE = f"{SUPABASE_URL}/storage/v1"
TILES_BUCKET = "kiln-tiles"
MANIFEST_OBJECT = "manifest.json"

TILE_CONTENT_TYPE = "image/png"
MANIFEST_CONTENT_TYPE = "application/json"
STATE_CONTENT_TYPE = "application/octet-stream"
TILE_URL_TEMPLATE = "{date}/{z}/{x}/{y}.png"

# A daily tile is never overwritten with different content -- a given date's
# z/x/y.png is written once and only ever re-uploaded with the same bytes on a
# re-run -- so daily tile uploads pass this as upload_tiles' cache_control.
# Nothing else does: the all-time pyramid's z/x/y.png at the same path *does*
# change, gaining a higher rank whenever a new reading beats the record
# already held there, and the frontend has no cache-busting on tile URLs to
# recover from a stale immutable cache. Manifests are republished daily and
# all-time state objects are overwritten with improved data, so they were
# already excluded on those grounds too.
TILE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# The all-time view. Permanent; never pruned.
ALLTIME_MANIFEST_OBJECT = "manifest-alltime.json"
ALLTIME_TILES_PREFIX = "alltime"
ALLTIME_STATE_PREFIX = "alltime-state"
ALLTIME_TILE_URL_TEMPLATE = f"{ALLTIME_TILES_PREFIX}/{{z}}/{{x}}/{{y}}.png"

# Two workers, not eight: the storage API shares the tortoise project's
# database connection pool with production apps, and the 651-job historical
# backfill drove it to 429 SlowDown at eight. Slow and polite wins here.
UPLOAD_WORKERS = 2
UPLOAD_ATTEMPTS = 5

# A handful of tiles lost to a flaky connection leaves a map with a few holes,
# which is survivable and visible. Losing more than this means the pyramid on
# disk is not the pyramid the manifest promises, and the run should go red.
MAX_TILE_FAILURE_RATE = 0.05

# Unused since ``prune_old_dates`` stopped deleting dated prefixes (decision
# 2026-09-03). Kept only as the default for ``prunable_date_prefixes``, which
# stays around -- and tested -- as a pure function for now.
KEEP_DATES = 2

DELETE_BATCH_SIZE = 500
LIST_PAGE_SIZE = 1000

# The list endpoint is not recursive, so pruning a date walks its z/x folders.
# The cap stops a pathological bucket from turning pruning into the whole run.
MAX_LIST_REQUESTS = 5000


class StorageWriteError(RuntimeError):
    """A write to the kiln-tiles bucket was rejected."""


def storage_headers(
    service_key: str, content_type: str | None = None, upsert: bool = True
) -> dict[str, str]:
    """Auth plus, for writes, the overwrite flag that makes re-running a date safe."""
    headers = {"Authorization": f"Bearer {service_key}"}
    if upsert:
        headers["x-upsert"] = "true"
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def tile_object_path(reading_date: date, zoom: int, tile_x: int, tile_y: int) -> str:
    return f"{reading_date.isoformat()}/{zoom}/{tile_x}/{tile_y}.png"


def build_manifest(
    reading_date: date,
    tile_count: int,
    generated_at: datetime | None = None,
    min_zoom: int = MIN_ZOOM,
    max_zoom: int = MAX_ZOOM,
) -> dict[str, Any]:
    """The manifest the frontend reads. Exactly these keys, in this order."""
    generated = generated_at or datetime.now(timezone.utc)
    return {
        "date": reading_date.isoformat(),
        "generated_at": generated.astimezone(timezone.utc).isoformat(),
        "min_zoom": int(min_zoom),
        "max_zoom": int(max_zoom),
        "tile_url_template": TILE_URL_TEMPLATE,
        "tile_count": int(tile_count),
    }


def alltime_tile_path(zoom: int, tile_x: int, tile_y: int) -> str:
    return f"{ALLTIME_TILES_PREFIX}/{zoom}/{tile_x}/{tile_y}.png"


def alltime_state_path(tile_x: int, tile_y: int) -> str:
    return f"{ALLTIME_STATE_PREFIX}/{tile_x}/{tile_y}.npy"


def build_alltime_manifest(
    since: str,
    through: date,
    tile_count: int,
    generated_at: datetime | None = None,
    min_zoom: int = MIN_ZOOM,
    max_zoom: int = MAX_ZOOM,
) -> dict[str, Any]:
    """The all-time manifest the frontend reads. Exactly these keys, in this order.

    ``since`` is carried forward from the previous manifest rather than
    recomputed: nothing else in the system remembers when the archive started.
    """
    generated = generated_at or datetime.now(timezone.utc)
    return {
        "since": since,
        "through": through.isoformat(),
        "generated_at": generated.astimezone(timezone.utc).isoformat(),
        "min_zoom": int(min_zoom),
        "max_zoom": int(max_zoom),
        "tile_url_template": ALLTIME_TILE_URL_TEMPLATE,
        "tile_count": int(tile_count),
    }


def is_date_prefix(name: str) -> bool:
    try:
        date.fromisoformat(name)
    except ValueError:
        return False
    return True


def prunable_date_prefixes(names: Iterable[str], keep: int = KEEP_DATES) -> list[str]:
    """Date folders to delete: everything but the ``keep`` most recent.

    Non-date entries (``manifest.json``) are ignored rather than treated as
    stale, so the manifest can never be pruned out from under the frontend.
    """
    dates = sorted({name for name in names if is_date_prefix(name)}, reverse=True)
    return sorted(dates[keep:])


@dataclass(frozen=True)
class UploadReport:
    uploaded: int
    failed: int

    @property
    def total(self) -> int:
        return self.uploaded + self.failed

    @property
    def failure_rate(self) -> float:
        return self.failed / self.total if self.total else 0.0

    @property
    def acceptable(self) -> bool:
        return self.failure_rate <= MAX_TILE_FAILURE_RATE


class StorageUploader:
    """Thin Storage REST client scoped to the kiln-tiles bucket.

    Uploads run on a small thread pool because a day's pyramid is thousands of
    tiny objects and the round trip, not the bytes, is the cost. Each worker
    gets its own session from ``session_factory``: ``requests.Session`` is not
    documented as thread-safe, and a shared connection pool is not worth the
    class of bug it invites.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        service_key: str,
        timeout: int = 60,
        max_workers: int = UPLOAD_WORKERS,
        attempts: int = UPLOAD_ATTEMPTS,
        sleep: Any = time.sleep,
    ):
        if not service_key:
            raise StorageWriteError("SUPABASE_SERVICE_KEY is empty")
        self._session_factory = session_factory
        self._service_key = service_key
        self._timeout = timeout
        self._max_workers = max_workers
        self._attempts = attempts
        self._sleep = sleep
        self._local = threading.local()

    def _session(self) -> Any:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            self._local.session = session
        return session

    def _request(
        self,
        method: str,
        url: str,
        content_type: str | None = None,
        upsert: bool = True,
        **kwargs: Any,
    ) -> Any:
        """One storage call, retrying transients at the single chokepoint.

        429 SlowDown (the shared pool's back-pressure), 5xx, and transport
        errors are retried with backoff; semantic 4xx fail immediately. Callers
        with their own retry loops just get extra patience, never less safety.
        """
        headers = storage_headers(self._service_key, content_type, upsert=upsert)
        headers.update(kwargs.pop("headers", {}) or {})
        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._session().request(
                    method, url, headers=headers, timeout=self._timeout, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - transport failures are retryable
                last_error = exc
                if attempt < self._attempts:
                    self._sleep(min(2.0 ** attempt, 30.0))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = StorageWriteError(
                    f"{method} {url} failed with {response.status_code}: {response.text[:500]}"
                )
                if attempt < self._attempts:
                    self._sleep(min(2.0 ** attempt, 30.0))
                continue
            if response.status_code >= 400:
                raise StorageWriteError(
                    f"{method} {url} failed with {response.status_code}: {response.text[:500]}"
                )
            return response
        raise StorageWriteError(
            f"{method} {url} failed after {self._attempts} attempts: {last_error}"
        )

    def download_object(self, path: str) -> bytes | None:
        """An object's bytes, or None when it does not exist yet.

        Supabase Storage reports a missing object as an HTTP 400 carrying a 404
        in its body, so absence is matched on both. Getting that wrong would
        turn "this tile is new" into a hard failure on every first run.

        Transient failures (429 SlowDown from the shared pool above all) are
        retried with the same backoff as uploads: absence is a fact, but a
        rate-limit is just a moment.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._session().request(
                    "GET",
                    f"{STORAGE_BASE}/object/{TILES_BUCKET}/{path}",
                    headers=storage_headers(self._service_key, upsert=False),
                    timeout=self._timeout,
                )
            except Exception as exc:  # noqa: BLE001 - transport failures are retryable
                last_error = exc
                if attempt < self._attempts:
                    self._sleep(min(2.0 ** attempt, 30.0))
                continue
            if response.status_code == 404:
                return None
            if response.status_code == 400 and "not_found" in response.text.lower().replace(
                " ", "_"
            ):
                return None
            if response.status_code >= 400:
                last_error = StorageWriteError(
                    f"GET {path} failed with {response.status_code}: {response.text[:500]}"
                )
                if attempt < self._attempts:
                    self._sleep(min(2.0 ** attempt, 30.0))
                continue
            return response.content
        raise StorageWriteError(f"GET {path} failed after {self._attempts} attempts: {last_error}")

    def download_objects(self, paths: Sequence[str]) -> dict[str, bytes | None]:
        """Fetch many objects on the upload pool. Raises if any fetch fails.

        Unlike a tile upload, a failed read is not tolerable: a state object
        that fails to load would look like an empty tile and silently reset that
        tile's all-time history.
        """
        if not paths:
            return {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            bodies = list(pool.map(self.download_object, paths))
        return dict(zip(paths, bodies))

    def upload_object(
        self,
        path: str,
        body: bytes,
        content_type: str,
        cache_control: str | None = None,
    ) -> None:
        headers = {"Cache-Control": cache_control} if cache_control else None
        self._request(
            "POST",
            f"{STORAGE_BASE}/object/{TILES_BUCKET}/{path}",
            content_type=content_type,
            data=body,
            headers=headers,
        )

    def _upload_tile(
        self, path: str, body: bytes, content_type: str, cache_control: str | None
    ) -> bool:
        # Retries (with backoff) live in _request, the single chokepoint; this
        # just converts an exhausted retry into a counted, tolerated failure.
        try:
            self.upload_object(path, body, content_type, cache_control=cache_control)
            return True
        except Exception as exc:  # noqa: BLE001 - counted by the caller's report
            LOG.warning("%s failed: %s", path, exc)
            return False

    def upload_tiles(
        self,
        objects: Sequence[tuple[str, bytes]],
        content_type: str = TILE_CONTENT_TYPE,
        cache_control: str | None = None,
    ) -> UploadReport:
        """Upload every object, tolerating individual failures.

        ``cache_control`` is opt-in per call rather than inferred from
        ``content_type``: a daily dated tile is never republished with
        different bytes at the same path, but an all-time pyramid tile is --
        the same z/x/y can get a higher rank whenever a new reading beats the
        record already stored there -- so the caller states which is true for
        the batch it is uploading. See ``TILE_CACHE_CONTROL``.
        """
        if not objects:
            return UploadReport(uploaded=0, failed=0)

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            outcomes = list(
                pool.map(
                    lambda item: self._upload_tile(*item, content_type, cache_control), objects
                )
            )

        uploaded = sum(1 for ok in outcomes if ok)
        return UploadReport(uploaded=uploaded, failed=len(outcomes) - uploaded)

    def upload_manifest(
        self, manifest: dict[str, Any], object_name: str = MANIFEST_OBJECT
    ) -> None:
        self.upload_object(
            object_name,
            json.dumps(manifest).encode("utf-8"),
            MANIFEST_CONTENT_TYPE,
        )

    def read_manifest(self, object_name: str) -> dict[str, Any] | None:
        """A published manifest, or None if it is absent or unreadable.

        Unreadable is treated as absent on purpose: the manifest is bookkeeping
        the run can rebuild, and refusing to publish today's archive because
        yesterday's JSON got mangled would be the worse failure.
        """
        body = self.download_object(object_name)
        if body is None:
            return None
        try:
            manifest = json.loads(body)
        except ValueError as exc:
            LOG.warning("%s is not readable JSON (%s); treating it as absent", object_name, exc)
            return None
        return manifest if isinstance(manifest, dict) else None

    def list_prefix(self, prefix: str) -> tuple[list[str], list[str]]:
        """Immediate children of ``prefix`` as ``(object names, folder names)``.

        The Storage list endpoint is one level deep: entries without an ``id``
        are folders rather than objects.
        """
        objects: list[str] = []
        folders: list[str] = []
        offset = 0
        while True:
            response = self._request(
                "POST",
                f"{STORAGE_BASE}/object/list/{TILES_BUCKET}",
                content_type=MANIFEST_CONTENT_TYPE,
                upsert=False,
                json={"prefix": prefix, "limit": LIST_PAGE_SIZE, "offset": offset},
            )
            page = response.json() or []
            for entry in page:
                name = str(entry.get("name", ""))
                if not name:
                    continue
                (objects if entry.get("id") else folders).append(name)
            if len(page) < LIST_PAGE_SIZE:
                return objects, folders
            offset += LIST_PAGE_SIZE

    def walk_objects(self, prefix: str) -> list[str]:
        """Every object key under ``prefix``, depth-first."""
        found: list[str] = []
        pending = [prefix]
        requests_made = 0

        while pending:
            current = pending.pop()
            if requests_made >= MAX_LIST_REQUESTS:
                LOG.warning(
                    "stopped listing %s after %d requests; some objects were left in place",
                    prefix,
                    requests_made,
                )
                break
            objects, folders = self.list_prefix(current)
            requests_made += 1
            base = f"{current}/" if current else ""
            found.extend(f"{base}{name}" for name in objects)
            pending.extend(f"{base}{name}" for name in folders)

        return found

    def delete_objects(self, paths: Sequence[str]) -> int:
        deleted = 0
        for start in range(0, len(paths), DELETE_BATCH_SIZE):
            batch = list(paths[start : start + DELETE_BATCH_SIZE])
            self._request(
                "DELETE",
                f"{STORAGE_BASE}/object/{TILES_BUCKET}",
                content_type=MANIFEST_CONTENT_TYPE,
                upsert=False,
                json={"prefixes": batch},
            )
            deleted += len(batch)
        return deleted

    def prune_old_dates(self, keep: int = KEEP_DATES) -> int:
        """No-op: dated tile prefixes are kept forever (decision 2026-09-03).

        Every day's raster pyramid now persists permanently, building a
        scrubbable archive. This deletes nothing, regardless of ``keep`` or how
        many date prefixes the bucket holds; it stays callable so the run
        orchestration in ``cli.py`` does not need to change.
        """
        return 0
