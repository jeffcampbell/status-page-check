"""RSS/Atom feed parsing for status page incident data."""

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime


def _clean_xml(xml_text):
    """Strip content:encoded blocks and XML namespaces for clean parsing.

    incident.io feeds duplicate the description in <content:encoded>;
    ElementTree handles namespaced elements awkwardly, so both are removed
    up front.
    """
    xml_text = re.sub(
        r"<content:encoded>.*?</content:encoded>", "", xml_text, flags=re.DOTALL
    )
    xml_text = re.sub(r'xmlns:\w+="[^"]*"', "", xml_text)
    return xml_text


def _strip_html(html_text):
    """Convert HTML to plain text."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = html.unescape(text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _parse_date(date_str):
    """Parse RFC 2822 date string, return datetime or None."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return None


def parse_rss(xml_text):
    """Parse an RSS 2.0 feed into a list of incident dicts.

    Returns list of dicts with keys:
        title, desc_raw, desc_text, pub_date, pub_date_str, link, guid
    """
    xml_text = _clean_xml(xml_text)
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item"):
        desc_raw = item.findtext("description", "").strip()
        pub_date_str = item.findtext("pubDate", "").strip()
        items.append(
            {
                "title": item.findtext("title", "").strip(),
                "desc_raw": desc_raw,
                "desc_text": _strip_html(desc_raw),
                "pub_date": _parse_date(pub_date_str),
                "pub_date_str": pub_date_str,
                "link": item.findtext("link", "").strip(),
                "guid": item.findtext("guid", "").strip(),
            }
        )
    return items


def parse_atom(xml_text):
    """Parse an Atom feed into a list of incident dicts.

    Atom feeds use <entry>, <summary>/<content>, and <updated> instead of
    RSS's <item>, <description>, and <pubDate>.
    """
    xml_text = _clean_xml(xml_text)
    # Strip the default Atom namespace so simple element names work
    xml_text = re.sub(r'xmlns="[^"]*"', "", xml_text)
    root = ET.fromstring(xml_text)
    items = []
    for entry in root.findall("entry"):
        desc_raw = (
            entry.findtext("content", "") or entry.findtext("summary", "")
        ).strip()
        date_str = (
            entry.findtext("updated", "") or entry.findtext("published", "")
        ).strip()
        link_el = entry.find("link")
        link = link_el.get("href", "") if link_el is not None else ""

        pub_date = None
        if date_str:
            try:
                pub_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except Exception:
                pub_date = _parse_date(date_str)

        items.append(
            {
                "title": entry.findtext("title", "").strip(),
                "desc_raw": desc_raw,
                "desc_text": _strip_html(desc_raw),
                "pub_date": pub_date,
                "pub_date_str": date_str,
                "link": link,
                "guid": entry.findtext("id", "").strip(),
            }
        )
    return items


def parse_feed_auto(xml_text):
    """Auto-detect RSS vs Atom and parse accordingly."""
    if "<feed" in xml_text[:2000]:
        return parse_atom(xml_text)
    return parse_rss(xml_text)


def incident_key(incident):
    """Stable identity for deduplicating an incident across feed snapshots."""
    ident = incident.get("link") or incident.get("guid")
    if ident:
        return ident.rstrip("/")
    date = incident["pub_date"].date().isoformat() if incident["pub_date"] else "?"
    return f"{incident['title'].lower().strip()}|{date}"


def merge_incidents(*incident_lists):
    """Merge incident lists, keeping the first occurrence of each incident.

    Pass the most authoritative list first (usually the live feed, then
    Wayback snapshots newest to oldest) — the version kept for a duplicated
    incident is the one from the earliest list it appears in.
    """
    seen = set()
    merged = []
    for incidents in incident_lists:
        for inc in incidents:
            key = incident_key(inc)
            if key in seen:
                continue
            seen.add(key)
            merged.append(inc)
    return merged


def extract_affected_components(desc_raw):
    """Extract affected component names from HTML description.

    Handles incident.io format: <li>ComponentName (Operational)</li>
    """
    components = re.findall(r"<li>(.*?)(?:\s*\(.*?\))?\s*</li>", desc_raw)
    return [html.unescape(c).strip() for c in components]


def extract_status(desc_raw):
    """Extract incident status from description text."""
    desc_lower = desc_raw.lower()
    for status in ["resolved", "complete", "monitoring", "identified", "investigating"]:
        if status in desc_lower:
            return status.capitalize()
    return "Other"


def extract_updates(desc_raw):
    """Extract individual status updates from an Atlassian Statuspage description.

    Atlassian Statuspage embeds all updates in the description HTML as:
        <p><small>Mon DD, HH:MM TZ</small><br><strong>Status</strong> - Message</p>

    Returns list of dicts with keys: status, message
    ordered from newest to oldest (as they appear in the feed).
    Returns empty list if the format doesn't match (e.g. incident.io feeds).
    """
    pattern = r"<strong>(.*?)</strong>\s*-\s*(.*?)(?=</p>|$)"
    matches = re.findall(pattern, desc_raw, re.DOTALL)
    if not matches:
        return []

    updates = []
    for status, message in matches:
        updates.append(
            {
                "status": status.strip(),
                "message": _strip_html(message).strip(),
            }
        )
    return updates


def extract_resolved_message(desc_raw):
    """Extract just the Resolved message from the description.

    Works for both Atlassian Statuspage format (<strong>Resolved</strong> - msg)
    and incident.io format (Status: Resolved msg).
    """
    updates = extract_updates(desc_raw)
    if updates:
        for u in updates:
            if u["status"].lower() == "resolved":
                return u["message"]
        # No explicit Resolved — return the first (newest) update
        return updates[0]["message"]

    # Fallback for incident.io format
    text = _strip_html(desc_raw)
    text = re.sub(r"^Status:\s*\w+\s*", "", text).strip()
    return text
