"""Client for the Radio Browser API (radio-browser.info) - a free, open,
community-maintained directory of internet radio stations with direct
stream URLs. Used to search for and add new stations from the web UI
instead of hand-editing stations.json.

Only stdlib is used here (urllib) - this is a light, occasional lookup, not
worth a new dependency (requests) for.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger("myndless.radio_browser")

Opener = Callable[..., object]  # matches urllib.request.urlopen's signature closely enough

# One of Radio Browser's stable mirror servers. Their own client libraries
# resolve a server list via DNS (all.api.radio-browser.info) and pick one at
# random for load balancing; a single fixed mirror is simpler and plenty for
# this project's light, on-demand usage. Swap if this one is ever down for
# good - the others (de2, nl1, at1, ...) share the same API shape.
API_BASE = "https://de1.api.radio-browser.info/json"

# The API's usage policy asks for a descriptive User-Agent identifying the
# calling application (not a browser UA) - see https://api.radio-browser.info
USER_AGENT = "myndless/0.1 (+https://github.com/unalut/myndless)"


def search_stations(
    query: str,
    limit: int = 20,
    timeout: float = 5.0,
    opener: Opener = urllib.request.urlopen,
) -> list[dict]:
    """Search Radio Browser by station name. Returns result dicts shaped for
    direct use by the web UI/orchestrator: stationuuid, name, url, country,
    tags, bitrate, favicon. Returns an empty list (logged, not raised) on
    any network/parse failure - a flaky lookup shouldn't break the page."""
    query = query.strip()
    if not query:
        return []

    params = urllib.parse.urlencode(
        {
            "name": query,
            "limit": limit,
            "hidebroken": "true",
            "order": "votes",
            "reverse": "true",
        }
    )
    url = f"{API_BASE}/stations/search?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        log.exception("radio-browser search failed for %r", query)
        return []

    return [
        {
            "stationuuid": item.get("stationuuid", ""),
            "name": item.get("name", ""),
            "url": item.get("url_resolved") or item.get("url", ""),
            "country": item.get("country", ""),
            "tags": item.get("tags", ""),
            "bitrate": item.get("bitrate", 0),
            "favicon": item.get("favicon", ""),
        }
        for item in data
        if item.get("url_resolved") or item.get("url")
    ]
