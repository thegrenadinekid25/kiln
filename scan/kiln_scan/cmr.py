"""Granule discovery for the daily global CMG products, through NASA's CMR.

MOD11C1 and MYD11C1 are one global file per UTC day, catalogued by CMR under the
LPCLOUD provider and distributed from ``data.lpdaac.earthdatacloud.nasa.gov``
behind an Earthdata bearer token. Verified against CMR on 2026-08-31: a
``short_name`` + ``version=061`` + one-day ``temporal`` query returns exactly one
entry per available day, whose ``data#`` link points at

    https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/
        MOD11C1.061/<granule>/<granule>.hdf

Query building and response parsing are pure functions so they can be tested
without network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping
from urllib.parse import urlparse

CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"

COLLECTION_VERSION = "061"

# CMR itself is a trusted first-party NASA API, not attacker input -- but a
# malformed or unexpected response should still never turn into an unrestricted
# outbound fetch. Every real granule host is nasa.gov or a subdomain of it;
# this is a defense-in-depth floor, not a workaround for anything broken today.
_GRANULE_HOST_SUFFIX = ".nasa.gov"


def _host_allowed(href: str) -> bool:
    host = (urlparse(href).hostname or "").lower()
    return host == "nasa.gov" or host.endswith(_GRANULE_HOST_SUFFIX)

# Exact CMR relation for a downloadable data file. Matched exactly rather than
# by suffix: "metadata#" also ends in "data#", and a suffix test would happily
# hand back the .cmr.xml sidecar.
DATA_REL = "http://esipfed.org/ns/fedsearch/1.1/data#"


@dataclass(frozen=True)
class ProductInfo:
    satellite: str
    #: First day the instrument produced this product. Days before it are not
    #: missing data; they are days the satellite was not yet flying it.
    record_start: date


PRODUCTS: dict[str, ProductInfo] = {
    "MOD11C1": ProductInfo(satellite="Terra", record_start=date(2000, 2, 24)),
    "MYD11C1": ProductInfo(satellite="Aqua", record_start=date(2002, 7, 4)),
}


class GranuleDiscoveryError(RuntimeError):
    """CMR was reachable but returned something we cannot use."""


@dataclass(frozen=True)
class GranuleRef:
    granule_id: str
    url: str
    #: The UTC day the granule covers, as CMR reports it.
    time_start: str


def product_info(product: str) -> ProductInfo:
    try:
        return PRODUCTS[product]
    except KeyError:
        raise ValueError(
            f"unknown product {product!r}; expected one of {sorted(PRODUCTS)}"
        ) from None


def temporal_range(target: date) -> str:
    """CMR temporal filter covering the whole UTC day."""
    return f"{target.isoformat()}T00:00:00Z,{target.isoformat()}T23:59:59Z"


def build_granule_query(product: str, target: date, page_size: int = 10) -> dict[str, Any]:
    """Query parameters for the one CMG granule covering ``target``.

    No ``provider`` filter. CMR currently serves these from LPCLOUD, but the
    provider a collection lives under is an archive-side detail that has moved
    before; filtering on it would turn a re-homing into a 24-year scan that
    silently finds nothing. Filtering on ``short_name`` and ``version`` names
    the science product itself, which cannot move.
    """
    product_info(product)  # validates the product name
    if page_size < 1:
        raise ValueError(f"page_size must be >= 1, got {page_size}")
    return {
        "short_name": product,
        "version": COLLECTION_VERSION,
        "temporal": temporal_range(target),
        "page_size": page_size,
        "sort_key": "start_date",
    }


def _data_links(entry: Mapping[str, Any]) -> list[str]:
    """HDF data links on a CMR entry, ignoring inherited collection-level ones."""
    links = []
    for link in entry.get("links", []) or []:
        if link.get("inherited"):
            continue
        if str(link.get("rel", "")) != DATA_REL:
            continue
        href = str(link.get("href", ""))
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
    occasionally lists a granule whose file has not been published yet, and the
    caller reads an empty list as "no data for this day", which is the same
    thing as far as the scan is concerned.
    """
    entries = feed.get("feed", {}).get("entry")
    if entries is None:
        raise GranuleDiscoveryError("CMR response has no feed.entry array")

    refs: list[GranuleRef] = []
    seen: set[str] = set()
    for entry in entries:
        links = _data_links(entry)
        if not links:
            continue
        granule_id = str(
            entry.get("producer_granule_id")
            or entry.get("title")
            or links[0].rsplit("/", 1)[-1]
        )
        if granule_id in seen:
            continue
        seen.add(granule_id)
        refs.append(
            GranuleRef(
                granule_id=granule_id,
                url=links[0],
                time_start=str(entry.get("time_start") or ""),
            )
        )
    return refs


def find_daily_granule(
    session: Any, product: str, target: date, timeout: int = 60
) -> GranuleRef | None:
    """The single CMG granule for ``target``, or None if the day has none.

    Days with no granule are ordinary in this record -- 2000-02-29 has none, for
    instance -- so an absent day is a return value, not an error.

    If CMR lists more than one, the first by start date wins and the rest are
    ignored: the products are defined as one file per day, and a reprocessing
    overlap is not a reason to fold the same day in twice.
    """
    params = build_granule_query(product, target)
    response = session.get(CMR_GRANULES_URL, params=params, timeout=timeout)
    response.raise_for_status()
    refs = parse_granule_entries(response.json())
    return refs[0] if refs else None
