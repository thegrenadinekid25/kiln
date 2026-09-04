"""Kiln historical MODIS LST scanner.

Pass 1 of Kiln's two-pass all-time scan: sweep every day of the MODIS record at
0.05-degree (~5.6 km) climate-modeling-grid resolution to find every (place,
day) that was ever extremely hot. Pass 2 -- a separate tool -- revisits the
candidates this produces at 1 km.

Deliberately standalone. It shares no code with ``ingest/`` (the daily
near-real-time pipeline): the two read different products off different NASA
hosts, and coupling a 24-year batch sweep to a job that must run every morning
would make each one's failures the other's problem.
"""

__version__ = "0.1.0"
