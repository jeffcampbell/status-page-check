"""Feed autodiscovery: turn a company name, domain, or status page URL into a feed URL."""

import re
from urllib.parse import urljoin, urlparse

from .jsonapi import try_statuspage_api
from .net import FetchError, http_get
from .rootly import try_rootly

# Common feed paths by provider:
#   incident.io: /feed.rss, /history.rss (older)
#   Atlassian Statuspage: /history.atom, /history.rss
#   Instatus: /feed.rss
COMMON_FEED_PATHS = [
    "/feed.rss",
    "/history.rss",
    "/history.atom",
    "/feed.atom",
    "/rss.xml",
    "/feed.xml",
    "/rss",
]

# Subdomains companies commonly use for status pages
STATUS_SUBDOMAINS = ["status", "trust", "health", "uptime"]

_FEED_LINK_RE = re.compile(
    r'<link\b[^>]*type="application/(?:rss|atom)\+xml"[^>]*>', re.IGNORECASE
)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


class DiscoveryError(Exception):
    """No usable feed could be found for the target."""


def is_feed_xml(text):
    """Heuristic: does this text look like an RSS or Atom feed?"""
    head = text[:2000]
    return "<rss" in head or "<feed" in head


def _looks_like_feed_url(url):
    path = urlparse(url).path.lower()
    return bool(re.search(r"\.(rss|atom|xml)$", path)) or path.rstrip("/").endswith(
        ("/feed", "/history", "/rss")
    )


def _candidate_bases(target):
    """Build a list of base URLs to try, most specific first."""
    if "://" not in target:
        target = "https://" + target
    parsed = urlparse(target)
    host = parsed.netloc.lower()
    bases = [f"{parsed.scheme}://{host}"]

    bare = re.sub(r"^www\.", "", host)
    if not any(bare.startswith(sub + ".") for sub in STATUS_SUBDOMAINS):
        for sub in STATUS_SUBDOMAINS:
            bases.append(f"https://{sub}.{bare}")
    return bases


def _feed_links_from_html(html_text, base_url):
    """Extract <link rel="alternate" type="application/rss+xml"> URLs from HTML."""
    urls = []
    for tag in _FEED_LINK_RE.findall(html_text):
        m = _HREF_RE.search(tag)
        if m:
            urls.append(urljoin(base_url, m.group(1)))
    return urls


def discover_feed(target, progress=lambda msg: None):
    """Resolve a target (URL, domain, or feed URL) to (feed_url, xml_text).

    Tries, in order:
    1. The target itself, if it already looks like a feed URL
    2. Feed <link> tags in the status page HTML
    3. Common provider feed paths
    Falls back from the given host to status.<domain>, trust.<domain>, etc.
    """
    target = target.strip().rstrip("/")

    # Direct feed URL?
    direct = target if "://" in target else "https://" + target
    if _looks_like_feed_url(direct):
        progress(f"Trying feed URL directly: {direct}")
        try:
            xml = http_get(direct)
            if is_feed_xml(xml):
                return direct, xml
        except FetchError:
            pass

    tried = []
    for base in _candidate_bases(target):
        # 1. Parse the page HTML for advertised feed links
        html_text = None
        try:
            progress(f"Checking {base} for advertised feeds...")
            html_text = http_get(base, retries=0)
        except FetchError:
            tried.append(base)
            continue

        if is_feed_xml(html_text):
            # The base URL itself served a feed
            return base, html_text

        for feed_url in _feed_links_from_html(html_text, base):
            try:
                xml = http_get(feed_url, retries=0)
                if is_feed_xml(xml):
                    progress(f"Found advertised feed: {feed_url}")
                    return feed_url, xml
            except FetchError:
                continue

        # 2. Probe common provider paths
        for path in COMMON_FEED_PATHS:
            feed_url = base + path
            try:
                xml = http_get(feed_url, retries=0)
            except FetchError:
                continue
            if is_feed_xml(xml):
                progress(f"Found feed at common path: {feed_url}")
                return feed_url, xml
        tried.append(base)

    raise DiscoveryError(
        f"No RSS/Atom feed found for '{target}'. Tried: {', '.join(tried) or target}. "
        "If you know the feed URL, pass it directly (e.g. https://status.example.com/feed.rss)."
    )


def discover_source(target, progress=lambda msg: None, prefer="auto"):
    """Resolve a target to the best available incident source.

    Prefers the Statuspage JSON API (declared severity, component tags,
    timestamped update history) and falls back to RSS/Atom feed discovery.

    Returns one of:
        {"type": "json", "api_url", "page_url", "incidents", "raw", "feed_url"}
        {"type": "feed", "feed_url", "xml"}

    For JSON sources, feed_url is the companion Atom feed when one exists —
    used for Wayback Machine history, which archives feeds far more often
    than API endpoints.
    """
    target = target.strip().rstrip("/")
    direct = target if "://" in target else "https://" + target

    # An explicit feed URL bypasses API probing
    if prefer != "json" and _looks_like_feed_url(direct):
        feed_url, xml = discover_feed(target, progress)
        return _feed_or_rootly(feed_url, xml, prefer, progress)

    if prefer in ("auto", "json"):
        for base in _candidate_bases(target):
            progress(f"Probing {base} for a Statuspage JSON API...")
            api = try_statuspage_api(base)
            if api:
                progress(
                    f"Found JSON API: {api['api_url']} "
                    f"({len(api['incidents'])} incidents)"
                )
                # Also locate the companion feed: it often reaches further
                # back than the API (and Wayback archives feeds, not APIs)
                feed_url, feed_xml = None, None
                try:
                    feed_url, feed_xml = discover_feed(api["page_url"], progress)
                except (DiscoveryError, FetchError):
                    pass
                return {
                    "type": "json",
                    "feed_url": feed_url,
                    "feed_xml": feed_xml,
                    **api,
                }
        if prefer == "json":
            raise DiscoveryError(
                f"No Statuspage JSON API found for '{target}' (tried "
                "/api/v2/incidents.json on candidate hosts). Try --source feed."
            )
        progress("No Statuspage JSON API found; falling back to feed discovery.")

    feed_url, xml = discover_feed(target, progress)
    return _feed_or_rootly(feed_url, xml, prefer, progress)


def _feed_or_rootly(feed_url, xml, prefer, progress):
    """Wrap a discovered feed as a plain feed source, upgrading it to a rich
    Rootly source when the page is Rootly-hosted.

    Rootly's feeds carry only a one-line summary per incident; the real
    timeline lives in the HTML detail pages, so 'auto' scrapes them for the
    same lifecycle/messaging analysis the JSON API path affords. '--source
    feed' opts out of that enrichment and takes the raw feed as-is.
    """
    if prefer != "feed":
        parsed = urlparse(feed_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rootly = try_rootly(base, feed_url=feed_url, feed_xml=xml, progress=progress)
        if rootly:
            return rootly
    return {"type": "feed", "feed_url": feed_url, "xml": xml}
