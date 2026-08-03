"""Rootly status page retrieval.

Rootly (rootly.com) hosts public status pages that are *not* Statuspage-
compatible: there is no ``/api/v2/incidents.json``. Instead a page exposes:

    /history.rss    RSS 2.0 incident history — the latest incidents, but each
    /history.atom   Atom variant — item carries only a one-line ``[Status]
                    summary`` description: no timeline, no components, no
                    severity.

The full per-incident timeline lives only in the HTML detail page
(``/incidents/<uuid>``), rendered as a Rootly "Updates" list. This module
fetches each incident's detail page and extracts the timestamped updates,
then normalizes everything into the shared incident dict shape used by the
rest of the pipeline (title, desc_raw rebuilt in the Atlassian
``<p><strong>Status</strong> - message</p>`` HTML shape so the messaging
regexes work unchanged, plus updates_timed / created_at / resolved_at) — so
Rootly pages get the same lifecycle + cadence + messaging analysis the
Statuspage JSON API path enables.

Quirks handled:
  * The status page root sits behind a Cloudflare JS challenge, but the feeds
    and detail pages do not — so retrieval never touches the challenged HTML.
  * A detail page's bare URL occasionally returns a Cloudflare challenge; the
    ``.rss`` suffix (which the Rails/Turbo app ignores, serving the same HTML)
    reliably returns the real page, so we request that variant.
  * All detail-page timestamps are rendered in UTC as
    ``February 27, 2026 at 10:11 PM UTC``.
"""

import html
import re
from datetime import datetime, timezone

from .net import FetchError, http_get
from .parser import _strip_html, parse_feed_auto

# A Rootly-hosted page brands its feed image/logo with a rootly.com asset URL.
_ROOTLY_MARKERS = ("rootly.com", "Rootly")

# One update block on a detail page:
#   <span class="text-base font-semibold text-green-500">Resolved</span>
#   <div class="... trix-content"><p ...>message html</p></div>
#   <div class="text-gray-700 text-sm"> February 27, 2026 at 10:11 PM UTC </div>
_UPDATE_RE = re.compile(
    r'font-semibold\s+text-[a-z]+-\d+"\s*>\s*([^<]+?)\s*</span>'   # status label
    r".*?trix-content\"\s*>(.*?)</div>\s*"                         # message inner html
    r'<div class="text-gray-\d+ text-sm"\s*>\s*(.*?)\s*</div>',    # timestamp
    re.DOTALL,
)

# The incident header carries the canonical start time (the "Updates" list may
# begin at the first public update, which can be a beat later):
#   <span>February 27, 2026 at 06:16 PM UTC</span>
_HEADER_TIME_RE = re.compile(
    r"<span>\s*([A-Z][a-z]+ \d{1,2}, \d{4} at \d{1,2}:\d{2}\s*[AP]M UTC)\s*</span>"
)

_RESOLVED_STATUSES = {"resolved", "completed", "complete", "postmortem"}


def is_rootly_feed(xml_text):
    """Heuristic: was this RSS/Atom feed served by a Rootly status page?"""
    head = xml_text[:4000]
    return any(marker in head for marker in _ROOTLY_MARKERS)


def _parse_detail_time(text):
    """Parse a Rootly detail-page timestamp (always UTC), or return None."""
    try:
        return datetime.strptime(
            text.strip(), "%B %d, %Y at %I:%M %p UTC"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_detail_html(link):
    """Fetch an incident detail page, preferring the challenge-proof ``.rss``
    variant. Returns the HTML, or None if it can't be retrieved."""
    candidates = []
    if not link.endswith(".rss"):
        candidates.append(link + ".rss")
    candidates.append(link)
    for url in candidates:
        try:
            body = http_get(url, retries=0)
        except FetchError:
            continue
        if "trix-content" in body:  # the Updates timeline is present
            return body
    return None


def _extract_timed_updates(detail_html):
    """Pull the timestamped update timeline out of a detail page.

    Returns a list of {status, message, message_html, at} sorted oldest-first;
    empty if the page has no recognizable Updates list.
    """
    updates = []
    for status, msg_html, ts in _UPDATE_RE.findall(detail_html):
        at = _parse_detail_time(ts)
        if at is None:
            continue
        updates.append(
            {
                "status": status.strip().capitalize(),
                "message": _strip_html(msg_html),
                "message_html": msg_html.strip(),
                "at": at,
            }
        )
    updates.sort(key=lambda u: u["at"])
    return updates


def _build_desc_raw(updates):
    """Rebuild the Atlassian-style description HTML the messaging regexes
    expect: newest update first, each ``<p><strong>Status</strong> - msg</p>``.
    The message's own HTML is preserved so structure checks (lists, headings)
    keep working."""
    return "".join(
        f"<p><strong>{u['status']}</strong> - {u['message_html']}</p>"
        for u in reversed(updates)
    )


def _enrich_from_detail(feed_incident, detail):
    """Build a rich incident dict from a feed item + its detail-page HTML.

    Pure (no network): pass detail=None (or HTML with no recognizable Updates
    list) to get the feed-summary fallback.
    """
    updates = _extract_timed_updates(detail) if detail else []

    if not updates:
        # Detail page unavailable: keep the feed item, but normalize its
        # "[Resolved] summary" body into the shared desc_raw shape so the
        # status is still recognized downstream.
        raw = feed_incident.get("desc_raw", "")
        m = re.match(r"\s*\[([^\]]+)\]\s*(.*)", _strip_html(raw), re.DOTALL)
        if m:
            status, summary = m.group(1).strip().capitalize(), m.group(2).strip()
            feed_incident = dict(feed_incident)
            feed_incident["desc_raw"] = (
                f"<p><strong>{status}</strong> - {html.escape(summary)}</p>"
            )
            feed_incident["desc_text"] = summary
        return feed_incident

    header = None
    if detail:
        hm = _HEADER_TIME_RE.search(detail)
        if hm:
            header = _parse_detail_time(hm.group(1))

    created_at = header or updates[0]["at"]
    resolved_at = next(
        (u["at"] for u in reversed(updates) if u["status"].lower() in _RESOLVED_STATUSES),
        None,
    )
    desc_raw = _build_desc_raw(updates)

    inc = dict(feed_incident)
    inc.update(
        {
            "desc_raw": desc_raw,
            "desc_text": _strip_html(desc_raw),
            # Rootly's feed <pubDate> tracks the *resolution* time (and is
            # occasionally a stale edit timestamp), not when the incident
            # began — so anchor the incident to its detail-page start. This
            # keeps frequency/trend charts on real dates and, since the first
            # public update *is* the disclosure, correctly makes pub_date ==
            # created_at (no false "disclosed late" backfill signal, which
            # would otherwise just measure incident duration).
            "pub_date": created_at,
            "pub_date_str": created_at.isoformat(),
            "created_at": created_at,
            "resolved_at": resolved_at,
            "updates_timed": [
                {"status": u["status"], "message": u["message"], "at": u["at"]}
                for u in updates
            ],
            # Rootly detail pages on this template disclose neither an affected-
            # component list nor an impact level, so we don't invent them.
            "components": feed_incident.get("components", []),
            "impact": None,
        }
    )
    return inc


def _enrich(feed_incident, progress):
    """Scrape a feed item's detail page and return a rich incident dict.
    Falls back to the feed's own one-liner if the page is unreachable."""
    link = feed_incident.get("link") or ""
    detail = _fetch_detail_html(link) if link else None
    inc = _enrich_from_detail(feed_incident, detail)
    n = len(inc.get("updates_timed") or [])
    progress(f"  · {inc['title'][:60]} ({n} update{'s' if n != 1 else ''})")
    return inc


def try_rootly(base_url, feed_url=None, feed_xml=None, progress=lambda msg: None):
    """Probe a base URL for a Rootly status page and return a rich source.

    Returns a source dict shaped like the JSON API path
    (``{"type": "rootly", "page_url", "incidents", "raw", "feed_url",
    "feed_xml"}``) or None if the page isn't Rootly-hosted or has no incidents.

    ``feed_url``/``feed_xml`` may be passed to reuse an already-fetched feed.
    """
    base = base_url.rstrip("/")
    if feed_xml is None:
        for path in ("/history.rss", "/history.atom"):
            try:
                feed_xml = http_get(base + path, retries=0)
            except FetchError:
                continue
            feed_url = base + path
            break
    if not feed_xml or not is_rootly_feed(feed_xml):
        return None

    feed_incidents = parse_feed_auto(feed_xml)
    if not feed_incidents:
        return None

    progress(f"Rootly status page detected; enriching {len(feed_incidents)} incidents from detail pages...")
    incidents = [_enrich(inc, progress) for inc in feed_incidents]
    return {
        "type": "rootly",
        "page_url": base,
        "incidents": incidents,
        "raw": feed_xml,
        "feed_url": feed_url,
        "feed_xml": feed_xml,
    }
