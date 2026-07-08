"""Incident frequency, trend, and component analysis."""

import json
import re
from collections import Counter
from datetime import datetime, timezone

from .parser import extract_affected_components, extract_status

# Keywords scanned in incident titles to characterize failure modes
DEFAULT_ISSUE_KEYWORDS = [
    "delay",
    "error",
    "fail",
    "degraded",
    "outage",
    "down",
    "slow",
    "timeout",
    "intermittent",
]

# Generic starting taxonomy; override per company via a TOML config
DEFAULT_COMPONENT_CATEGORIES = {
    "API": ["api", "endpoint", "rest", "graphql", "rate limit"],
    "Authentication": ["auth", "login", "sso", "oauth", "saml", "mfa", "sign-in", "sign in"],
    "Dashboard / UI": ["dashboard", "ui", "console", "portal", "editor", "website"],
    "Data / Storage": ["database", "storage", "s3", "redis", "cache", "backup"],
    "Email / Notifications": ["email", "notification", "sms", "webhook delivery"],
    "Infrastructure": ["infrastructure", "deploy", "dns", "cdn", "network", "latency", "capacity"],
    "Integrations": ["webhook", "integration", "connector", "third-party", "third party"],
    "Billing": ["billing", "payment", "invoice", "subscription", "checkout"],
}


def classify_component(title, description, categories):
    """Classify an incident into a component category using keyword matching.

    Keywords match on word boundaries — a plain substring check would make
    "api" match inside "Zapier" or "rapid".
    """
    text = (title + " " + description).lower()
    for category, keywords in categories.items():
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                return category
    return "Other"


def compute_stats(incidents, label, categories):
    """Compute a stats dict for a set of incidents.

    Returns None if no incident has a parseable date.
    """
    dates = sorted(i["pub_date"] for i in incidents if i["pub_date"])
    if not dates:
        return None
    span_days = max((dates[-1] - dates[0]).days, 1)

    monthly = Counter()
    dow = Counter()
    component_counts = Counter()
    keyword_counts = Counter()
    affected = Counter()

    for i in incidents:
        cat = classify_component(i["title"], i["desc_raw"], categories)
        component_counts[cat] += 1
        i["category"] = cat
        i["status"] = extract_status(i["desc_raw"])

        if i["pub_date"]:
            monthly[i["pub_date"].strftime("%Y-%m")] += 1
            dow[i["pub_date"].strftime("%A")] += 1

        title_lower = i["title"].lower()
        for word in DEFAULT_ISSUE_KEYWORDS:
            if word in title_lower:
                keyword_counts[word] += 1

        for comp in extract_affected_components(i["desc_raw"]):
            affected[comp] += 1

    return {
        "label": label,
        "count": len(incidents),
        "date_start": dates[0].strftime("%Y-%m-%d"),
        "date_end": dates[-1].strftime("%Y-%m-%d"),
        "span_days": span_days,
        "per_week": len(incidents) / (span_days / 7),
        "monthly": monthly,
        "dow": dow,
        "components": component_counts,
        "keywords": keyword_counts,
        "affected": affected,
        "incidents": incidents,
    }


def split_periods(incidents, min_span_days=360, min_per_period=4):
    """Split incidents at the midpoint of their date range for period comparison.

    Returns ((older, older_label), (newer, newer_label)) or None if the data
    doesn't span long enough (or either half would be too small) for a
    meaningful comparison.
    """
    dated = sorted(
        (i for i in incidents if i["pub_date"]), key=lambda i: i["pub_date"]
    )
    if len(dated) < min_per_period * 2:
        return None
    start, end = dated[0]["pub_date"], dated[-1]["pub_date"]
    if (end - start).days < min_span_days:
        return None

    mid = start + (end - start) / 2
    older = [i for i in dated if i["pub_date"] < mid]
    newer = [i for i in dated if i["pub_date"] >= mid]
    if len(older) < min_per_period or len(newer) < min_per_period:
        return None

    older_label = f"{start.strftime('%b %Y')} – {mid.strftime('%b %Y')}"
    newer_label = f"{mid.strftime('%b %Y')} – {end.strftime('%b %Y')}"
    return (older, older_label), (newer, newer_label)


def export_json(stats_list, output_path):
    """Export incident data from one or more stats dicts to JSON."""
    export = {}
    for stats in stats_list:
        sorted_inc = sorted(
            stats["incidents"],
            key=lambda x: x["pub_date"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        export[stats["label"]] = [
            {
                "title": i["title"],
                "date": i["pub_date"].isoformat() if i["pub_date"] else None,
                "category": i.get("category"),
                "status": i.get("status"),
                "link": i["link"],
            }
            for i in sorted_inc
        ]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2)
