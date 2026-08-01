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
MIN_CONNECTION_TIME_MINUTES = 5
# Cap on how long a wait at the interchange is worth showing — an interchange
# journey with a 3-hour layover isn't a useful "1 change" result even though
# it's technically valid; see RESEARCH.md §3.
MAX_CONNECTION_TIME_MINUTES = 90
