"""Kiln land-surface-temperature ingestion pipeline.

Daily job: discover NASA LANCE near-real-time MODIS LST granules for a target
date, reduce them to the hottest 1-degree tiles worldwide, and upsert those
tiles into the kiln schema in Supabase.
"""

__version__ = "0.1.0"
