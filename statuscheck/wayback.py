"""Historical feed retrieval via the Internet Archive Wayback Machine.

Status page feeds are rolling windows (typically the last N incidents or
months), so the live feed alone can't show long-term trends. The Wayback
Machine often holds older snapshots of the same feed URL; merging them
extends the analysis window and enables period-over-period comparison.
"""

import json
import time
from urllib.parse import quote

from .discover import is_feed_xml
from .net import FetchError, http_get

CDX_API = "https://web.archive.org/cdx/search/cdx"


def list_snapshots(feed_url, timeout=60):
    """Return snapshot timestamps for a URL, collapsed to one per month."""
    query = (
        f"{CDX_API}?url={quote(feed_url, safe='')}"
        "&output=json&fl=timestamp&filter=statuscode:200&collapse=timestamp:6"
    )
    body = http_get(query, timeout=timeout)
    if not body.strip():
        return []
    rows = json.loads(body)
    return [row[0] for row in rows[1:]]  # rows[0] is the header


def select_spread(timestamps, max_n):
    """Pick up to max_n timestamps spread evenly across the list (always
    including the earliest and latest)."""
    if len(timestamps) <= max_n:
        return list(timestamps)
    picked = []
    for i in range(max_n):
        idx = round(i * (len(timestamps) - 1) / (max_n - 1))
        ts = timestamps[idx]
        if ts not in picked:
            picked.append(ts)
    return picked


def fetch_snapshot(feed_url, timestamp, timeout=60):
    """Fetch the raw archived feed content for one snapshot.

    The id_ URL variant returns the original bytes; without it the Wayback
    Machine wraps the response in its own HTML chrome.
    """
    url = f"https://web.archive.org/web/{timestamp}id_/{feed_url}"
    return http_get(url, timeout=timeout)


def fetch_history(feed_url, max_snapshots=8, delay=1.0, progress=lambda msg: None):
    """Fetch a spread of archived copies of feed_url.

    Returns a list of (timestamp, xml_text), oldest first. Snapshots that
    fail to download or don't parse as feeds are skipped.
    """
    progress("Querying Wayback Machine for archived feed snapshots...")
    try:
        timestamps = list_snapshots(feed_url)
    except (FetchError, json.JSONDecodeError) as e:
        progress(f"  Wayback lookup failed ({e}); continuing without history.")
        return []

    if not timestamps:
        progress("  No archived snapshots found.")
        return []

    chosen = select_spread(timestamps, max_snapshots)
    progress(
        f"  {len(timestamps)} monthly snapshots available "
        f"({timestamps[0][:6]} to {timestamps[-1][:6]}); fetching {len(chosen)}."
    )

    results = []
    for i, ts in enumerate(chosen):
        if i > 0:
            time.sleep(delay)  # be polite to the archive
        try:
            xml = fetch_snapshot(feed_url, ts)
        except FetchError as e:
            progress(f"  Snapshot {ts}: fetch failed, skipping ({e})")
            continue
        if not is_feed_xml(xml):
            progress(f"  Snapshot {ts}: not a feed (page may have been archived as HTML), skipping")
            continue
        progress(f"  Snapshot {ts[:8]}: ok")
        results.append((ts, xml))
    return results
