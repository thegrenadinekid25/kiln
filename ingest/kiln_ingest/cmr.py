"""Granule discovery through NASA's Common Metadata Repository.

The near-real-time MODIS LST granules are catalogued in CMR under the
LANCEMODIS provider; the actual HDF files live on the LANCE distribution hosts
(nrt3/nrt4.modaps.eosdis.nasa.gov) and need an Earthdata bearer token.

Query building and response parsing are pure functions so they can be tested
without network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlparse

CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"

# CMR itself is a trusted first-party NASA API, not attacker input -- but a
# malformed or unexpected response should still never turn into an unrestricted
# outbound fetch. Every real granule host is nasa.gov or a subdomain of it
# (LANCE's modaps.eosdis.nasa.gov, the archive's lpdaac.earthdatacloud.nasa.gov);
# this is a defense-in-depth floor, not a workaround for anything broken today.
_GRANULE_HOST_SUFFIX = ".nasa.gov"


def _host_allowed(href: str) -> bool:
    host = (urlparse(href).hostname or "").lower()
    return host == "nasa.gov" or host.endswith(_GRANULE_HOST_SUFFIX)

# Near-real-time provider, for the daily run. LANCE publishes within about
# three hours of observation and keeps only the recent past.
NRT_PROVIDER = "LANCEMODIS"

# Science-quality archive, for historical backfills. Verified against CMR on
# 2026-08-31: for 2019-07-15, LPCLOUD is the only provider carrying MOD11_L2 --
# LPDAAC_ECS, LAADS and LANCEMODIS all return nothing -- and it serves MOD14 and
# MYD14 under the same short names the NRT feed uses. Data links are direct
# HTTPS .hdf under data.lpdaac.earthdatacloud.nasa.gov and accept an Earthdata
# bearer token.
ARCHIVE_PROVIDER = "LPCLOUD"

# Collection 6.1, the full-mission reprocessing: spot-checked as present for
# 2000-03-05, 2002-08-15, 2010-06-20 and 2026-01-10. Pinned rather than left
# open because a backfill spans years, and a future collection appearing
# mid-run would silently return two granules per overpass instead of one.
ARCHIVE_VERSION = "061"

# CMR caps page_size at 2000.
MAX_PAGE_SIZE = 2000

PRODUCTS: dict[str, str] = {
    "MOD11_L2": "Terra",
    "MYD11_L2": "Aqua",
}

# The active-fire product flown on the same satellite. MOD14/MYD14 are produced
# from the same 5-minute overpasses as MOD11/MYD11, so every LST granule has at
# most one fire granule and they are paired by the overpass stamp in the name.
# Note the asymmetric naming: the LST products carry an _L2 suffix in CMR but
# the fire products do not (short names MOD14/MYD14, verified against CMR).
FIRE_PRODUCTS: dict[str, str] = {
    "MOD11_L2": "MOD14",
    "MYD11_L2": "MYD14",
}

# ``A2026242.1125``: acquisition year, day of year and UTC start minute,
# identical across products of one overpass. The two feeds name granules
# differently after that -- NRT ends ``.061.NRT.hdf`` and the archive ends
# ``.061.<production timestamp>.hdf`` -- but the stamp sits in the same place in
# both, which is what lets one pairing rule serve a daily run and a backfill.
TIME_KEY_PATTERN = re.compile(r"\.(A\d{7}\.\d{4})\.")

# west, south, east, north, in degrees.
BoundingBox = tuple[float, float, float, float]


class GranuleDiscoveryError(RuntimeError):
    """CMR was reachable but returned something we cannot use."""


@dataclass(frozen=True)
class GranuleRef:
    granule_id: str
    url: str
    observed_at: str


def satellite_for_product(product: str) -> str:
    try:
        return PRODUCTS[product]
    except KeyError:
        raise ValueError(
            f"unknown product {product!r}; expected one of {sorted(PRODUCTS)}"
        ) from None


def fire_product_for(product: str) -> str:
    """The active-fire product paired with an LST product."""
    try:
        return FIRE_PRODUCTS[product]
    except KeyError:
        raise ValueError(
            f"unknown product {product!r}; expected one of {sorted(FIRE_PRODUCTS)}"
        ) from None


def product_from_granule_id(granule_id: str) -> str | None:
    """The LST product a granule filename names, or None if it names no known one.

    Granule ids are ``MOD11_L2.A2026242.1125.061.NRT.hdf``: the product is the
    first dotted field. The all-time table needs it because its rows are merged
    across both satellites and no longer sit inside a per-product loop.
    """
    head = granule_id.split(".", 1)[0]
    return head if head in PRODUCTS else None


def granule_time_key(granule_id: str) -> str | None:
    """The ``AYYYYDDD.HHMM`` overpass stamp, or None if the name has no such stamp."""
    match = TIME_KEY_PATTERN.search(granule_id)
    return match.group(1) if match else None


def temporal_range(target: date) -> str:
    """CMR temporal filter covering the whole UTC day."""
    return f"{target.isoformat()}T00:00:00Z,{target.isoformat()}T23:59:59Z"


def _coordinate(value: float) -> str:
    """A bounding-box degree, without the trailing zeros CMR does not need."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_bounding_box(bbox: BoundingBox) -> str:
    """CMR's ``west,south,east,north`` spatial filter."""
    return ",".join(_coordinate(value) for value in bbox)


def _base_query(
    short_name: str,
    target: date,
    page_size: int,
    page_num: int,
    day_night: str | None,
    archive: bool = False,
    bboxes: Sequence[BoundingBox] = (),
) -> dict[str, Any]:
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be 1..{MAX_PAGE_SIZE}, got {page_size}")
    if page_num < 1:
        raise ValueError(f"page_num must be >= 1, got {page_num}")

    query: dict[str, Any] = {
        "short_name": short_name,
        "provider": ARCHIVE_PROVIDER if archive else NRT_PROVIDER,
        "temporal": temporal_range(target),
        "page_size": page_size,
        "page_num": page_num,
        "sort_key": "start_date",
    }
    if archive:
        query["version"] = ARCHIVE_VERSION
    if day_night is not None:
        query["day_night_flag"] = day_night

    if bboxes:
        query["bounding_box"] = [format_bounding_box(bbox) for bbox in bboxes]
        if len(bboxes) > 1:
            # Repeated spatial filters are ANDed by default, which for disjoint
            # regions matches nothing at all. Verified against CMR: two disjoint
            # boxes return 0 granules without this and 2 with it.
            query["options[bounding_box][or]"] = "true"

    return query


def build_granule_query(
    product: str,
    target: date,
    page_size: int = MAX_PAGE_SIZE,
    page_num: int = 1,
    archive: bool = False,
    bboxes: Sequence[BoundingBox] = (),
) -> dict[str, Any]:
    """Query parameters for one page of daytime LST granules."""
    satellite_for_product(product)  # validates the product name
    return _base_query(
        product, target, page_size, page_num, day_night="day", archive=archive, bboxes=bboxes
    )


def build_fire_query(
    fire_product: str,
    target: date,
    page_size: int = MAX_PAGE_SIZE,
    page_num: int = 1,
    archive: bool = False,
    bboxes: Sequence[BoundingBox] = (),
) -> dict[str, Any]:
    """Query parameters for one page of active-fire granules.

    Deliberately unfiltered by day/night flag: the pairing is by overpass stamp,
    and a fire granule CMR happens to flag differently from its LST twin would
    otherwise silently drop the mask for that overpass.
    """
    return _base_query(
        fire_product, target, page_size, page_num, day_night=None, archive=archive, bboxes=bboxes
    )


def build_granule_query_string(
    product: str,
    target: date,
    page_size: int = MAX_PAGE_SIZE,
    page_num: int = 1,
    archive: bool = False,
    bboxes: Sequence[BoundingBox] = (),
) -> str:
    # doseq, because a multi-region query repeats bounding_box.
    return urlencode(
        build_granule_query(product, target, page_size, page_num, archive, bboxes),
        doseq=True,
    )


def _data_links(entry: Mapping[str, Any]) -> list[str]:
    """HTTP data links on a CMR entry, ignoring inherited collection-level ones."""
    links = []
    for link in entry.get("links", []) or []:
        if link.get("inherited"):
            continue
        rel = str(link.get("rel", ""))
        href = str(link.get("href", ""))
        if not rel.endswith("/data#"):
            continue
        if not href.lower().startswith("http"):
            continue
        if not href.lower().endswith(".hdf"):
            continue
        if not _host_allowed(href):
            continue
        links.append(href)
    return links


def parse_granule_entries(feed: Mapping[str, Any]) -> list[GranuleRef]:
    """Turn a CMR granules.json body into downloadable granule references.

    Entries without a usable HDF link are skipped rather than raising: CMR
    occasionally lists a granule whose file has not been published yet, and one
    such entry must not sink the whole day.
    """
    entries = feed.get("feed", {}).get("entry")
    if entries is None:
        raise GranuleDiscoveryError("CMR response has no feed.entry array")

    refs: list[GranuleRef] = []
    for entry in entries:
        links = _data_links(entry)
        if not links:
            continue
        granule_id = str(
            entry.get("producer_granule_id") or entry.get("title") or links[0].rsplit("/", 1)[-1]
        )
        observed_at = str(entry.get("time_start") or entry.get("updated") or "")
        if not observed_at:
            continue
        refs.append(GranuleRef(granule_id=granule_id, url=links[0], observed_at=observed_at))
    return refs


def dedupe_granules(refs: Sequence[GranuleRef]) -> list[GranuleRef]:
    """Drop repeated granule ids, keeping first occurrence and input order."""
    seen: set[str] = set()
    out: list[GranuleRef] = []
    for ref in refs:
        if ref.granule_id in seen:
            continue
        seen.add(ref.granule_id)
        out.append(ref)
    return out


def time_key_map(refs: Sequence[GranuleRef]) -> dict[str, str]:
    """Overpass stamp -> download URL, for pairing one product against another.

    Names without a parseable stamp are skipped; nothing can be paired with them
    anyway.
    """
    mapping: dict[str, str] = {}
    for ref in refs:
        key = granule_time_key(ref.granule_id)
        if key is not None:
            mapping.setdefault(key, ref.url)
    return mapping


def _search_pages(
    session: Any,
    build_query: Callable[[int, int], dict[str, Any]],
    page_size: int,
    max_pages: int,
    timeout: int,
) -> list[GranuleRef]:
    """Page through CMR until it stops returning granules."""
    collected: list[GranuleRef] = []
    for page_num in range(1, max_pages + 1):
        params = build_query(page_size, page_num)
        response = session.get(CMR_GRANULES_URL, params=params, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        collected.extend(parse_granule_entries(body))
        # Page against the raw entry count, not the parsed one: entries we skip
        # for want of a data link would otherwise look like the end of the feed.
        if len(body.get("feed", {}).get("entry") or []) < page_size:
            break
    return dedupe_granules(collected)


def search_granules(
    session: Any,
    product: str,
    target: date,
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int = 10,
    timeout: int = 60,
    archive: bool = False,
    bboxes: Sequence[BoundingBox] = (),
) -> list[GranuleRef]:
    """Every daytime LST granule CMR lists for the target date."""
    return _search_pages(
        session,
        lambda size, num: build_granule_query(
            product, target, page_size=size, page_num=num, archive=archive, bboxes=bboxes
        ),
        page_size,
        max_pages,
        timeout,
    )


def search_fire_granules(
    session: Any,
    product: str,
    target: date,
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int = 10,
    timeout: int = 60,
    archive: bool = False,
    bboxes: Sequence[BoundingBox] = (),
) -> list[GranuleRef]:
    """Every active-fire granule for the satellite that flies ``product``."""
    fire_product = fire_product_for(product)
    return _search_pages(
        session,
        lambda size, num: build_fire_query(
            fire_product, target, page_size=size, page_num=num, archive=archive, bboxes=bboxes
        ),
        page_size,
        max_pages,
        timeout,
    )
