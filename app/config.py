import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
GTFS_DB_PATH = DATA_DIR / "gtfs.db"
GTFS_DOWNLOAD_URL = os.environ.get(
    "GTFS_DOWNLOAD_URL",
    "https://storage.travelwhiz.app/generated-gtfs/gb-nationalrail.gtfs.zip",
)

# Daily refresh time (24h, server-local). TravelWhiz regenerates the feed nightly
# between 22:00-00:00; 04:00 gives comfortable margin after that window closes.
REFRESH_HOUR = int(os.environ.get("REFRESH_HOUR", "4"))
REFRESH_MINUTE = int(os.environ.get("REFRESH_MINUTE", "0"))

DEFAULT_WINDOW_MINUTES = 60
# /api/direct is a plain indexed range scan regardless of window size, so it
# keeps the generous 24h cap. /api/journeys and /results additionally do one
# leg-2 query per leg-1 candidate (see queries.find_interchange_trips) — an
# unbounded window there risks a very slow request and exhausting uvicorn's
# threadpool under concurrent load; found in code review, 2026-08-01.
MAX_JOURNEYS_WINDOW_MINUTES = 180
MIN_CONNECTION_TIME_MINUTES = 5
# Cap on how long a wait at the interchange is worth showing — an interchange
# journey with a 3-hour layover isn't a useful "1 change" result even though
# it's technically valid; see RESEARCH.md §3.
MAX_CONNECTION_TIME_MINUTES = 90

# Maps a station's primary, public-facing CRS code to other codes TravelWhiz's
# GTFS conversion assigns to the *same physical station complex* — found via
# GitHub issue #11 (2026-08-01): Paddington and Liverpool Street's Elizabeth
# line platforms have their own NaPTAN/ATCO stop, so the CIF/NaPTAN -> GTFS
# conversion synthesizes a second pseudo-CRS code (PDX/LSX) for them, distinct
# from the mainline code (PAD/LST) Darwin/National Rail Enquiries and
# ordinary riders actually use/search for. The primary code is the key so
# station-list/autocomplete code can hide the alias without guessing which
# side of the pair is "real". A scan of the full feed (issue #11) found these
# are the only two such splits — no other Elizabeth line stop, and none of
# the older DPRS-era multi-CRS stations (Ebbsfleet, Glasgow Central, St
# Pancras, etc.), have a duplicate code in this feed. Tied to TravelWhiz's
# specific conversion pipeline, not a general GTFS/CRS fact — worth
# rechecking if the feed source ever changes.
STATION_ALIASES: dict[str, tuple[str, ...]] = {
    "PAD": ("PDX",),  # London Paddington <- Paddington (Elizabeth line)
    "LST": ("LSX",),  # London Liverpool Street <- Liverpool Street (Elizabeth line)
}
