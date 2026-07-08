"""Statuspage JSON API retrieval.

Atlassian Statuspage exposes a public JSON API alongside its Atom feed:

    /api/v2/incidents.json    the 50 most recent incidents, with declared
                              impact level, component tags, and the full
                              timestamped update history
    /api/v2/components.json   the page's component list

This is a much richer source than the feeds — which lack severity and
per-update timestamps — so it is preferred when present.
"""

import json
from datetime import datetime

from .net import FetchError, http_get
from .parser import _strip_html


def _iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_statuspage_json(data, fallback_base):
    """Normalize a decoded incidents.json payload into incident dicts.

    Dicts are shaped like the feed parser's output (title, desc_raw,
    desc_text, pub_date, link, guid) — desc_raw is rebuilt in the same
    HTML shape the Atom feeds use so all messaging regexes work
    unchanged — plus structured extras: impact, created_at, resolved_at,
    updates_timed, components.
    """
    page_url = ((data.get("page") or {}).get("url") or fallback_base).rstrip("/")
    incidents = []
    for inc in data.get("incidents", []):
        updates = inc.get("incident_updates") or []
        desc_raw = "".join(
            f"<p><strong>{(u.get('status') or '').capitalize()}</strong>"
            f" - {u.get('body') or ''}</p>"
            for u in updates
        )
        updates_timed = [
            {
                "status": (u.get("status") or "").capitalize(),
                "message": _strip_html(u.get("body") or ""),
                "at": _iso(u.get("display_at") or u.get("created_at")),
            }
            for u in updates
        ]
        updates_timed = sorted(
            (u for u in updates_timed if u["at"]), key=lambda u: u["at"]
        )
        incidents.append(
            {
                "title": (inc.get("name") or "").strip(),
                "desc_raw": desc_raw,
                "desc_text": _strip_html(desc_raw),
                "pub_date": _iso(inc.get("created_at")),
                "pub_date_str": inc.get("created_at") or "",
                "link": f"{page_url}/incidents/{inc.get('id')}",
                "guid": inc.get("id") or "",
                "impact": inc.get("impact"),
                "created_at": _iso(inc.get("started_at") or inc.get("created_at")),
                "resolved_at": _iso(inc.get("resolved_at")),
                "updates_timed": updates_timed,
                "components": [
                    c.get("name", "") for c in (inc.get("components") or [])
                ],
            }
        )
    return page_url, incidents


def try_statuspage_api(base_url):
    """Probe a base URL for a Statuspage JSON API.

    Returns {"api_url", "page_url", "incidents", "raw"} or None if the
    endpoint is missing, malformed, or empty.
    """
    api_url = base_url.rstrip("/") + "/api/v2/incidents.json"
    try:
        body = http_get(api_url, retries=0)
        data = json.loads(body)
    except (FetchError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "incidents" not in data:
        return None
    page_url, incidents = parse_statuspage_json(data, base_url)
    if not incidents:
        return None
    return {
        "api_url": api_url,
        "page_url": page_url,
        "incidents": incidents,
        "raw": body,
    }


def fetch_component_names(page_url):
    """Fetch the page's component list (leaf components, not groups)."""
    url = page_url.rstrip("/") + "/api/v2/components.json"
    try:
        data = json.loads(http_get(url, retries=0))
        return [
            c["name"]
            for c in data.get("components", [])
            if c.get("name") and not c.get("group")
        ]
    except (FetchError, json.JSONDecodeError, TypeError, KeyError):
        return []
