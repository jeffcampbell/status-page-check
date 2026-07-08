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
    severity = Counter()

    for i in incidents:
        # JSON-sourced incidents carry structured component tags that
        # aren't in the description text — include them in classification
        components = i.get("components") or extract_affected_components(i["desc_raw"])
        cat = classify_component(
            i["title"], i["desc_raw"] + " " + " ".join(components), categories
        )
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

        for comp in components:
            affected[comp] += 1

        if i.get("impact"):
            severity[i["impact"]] += 1

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
        "severity": severity,
        "incidents": incidents,
    }


def filter_focus(incidents, terms):
    """Filter incidents to those matching focus terms (case-insensitive
    substring) against the title, classified category, and component tags.

    Used for vendor evaluation: "I only depend on their API and webhooks."
    Call after compute_stats so categories are assigned.
    """
    terms = [t.strip().lower() for t in terms if t.strip()]
    if not terms:
        return incidents
    matched = []
    for i in incidents:
        components = i.get("components") or extract_affected_components(i["desc_raw"])
        haystack = " ".join([i.get("category") or "", i["title"], *components]).lower()
        if any(t in haystack for t in terms):
            matched.append(i)
    return matched


_POSTMORTEM_RE = re.compile(
    r"(?i)post-?mortem|root cause analysis|detailed analysis|incident review|\brca\b"
)
_ROOT_CAUSE_RE = re.compile(r"(?i)root cause|caused by")

# An incident posted this long after its declared start counts as backfilled
_BACKFILL_THRESHOLD_HOURS = 24


def analyze_transparency(incidents):
    """Measure how forthcoming the page is: severity usage, postmortem and
    root-cause rates, and late disclosure (backfilling). Facts only —
    interpretation is left to the report/LLM."""
    n = len(incidents)
    if n == 0:
        return None

    with_severity = [i for i in incidents if i.get("impact")]
    major = [i for i in with_severity if i["impact"] in ("critical", "major")]

    # Postmortems matter most for major incidents; measure there when
    # severity data exists, otherwise across everything
    pm_basis, pm_label = (major, "major/critical incidents") if major else (
        incidents, "all incidents"
    )
    postmortem_rate = (
        sum(1 for i in pm_basis if _POSTMORTEM_RE.search(i["desc_raw"]))
        / len(pm_basis) * 100
    )

    # Late disclosure: the incident was posted (pub_date) long after its
    # declared start (created_at, backdatable on Statuspage). Only
    # measurable for JSON-sourced incidents that carry both.
    backfilled = []
    backfill_measurable = 0
    for i in incidents:
        started, posted = i.get("created_at"), i.get("pub_date")
        if started and posted:
            backfill_measurable += 1
            delay_h = (posted - started).total_seconds() / 3600
            if delay_h >= _BACKFILL_THRESHOLD_HOURS:
                backfilled.append((i["title"], delay_h))

    return {
        "count": n,
        "severity_coverage_pct": len(with_severity) / n * 100,
        "major_count": len(major),
        "never_above_minor": bool(with_severity) and not major,
        "postmortem_rate_pct": postmortem_rate,
        "postmortem_basis": pm_label,
        "root_cause_major_pct": (
            sum(1 for i in major if _ROOT_CAUSE_RE.search(i["desc_raw"]))
            / len(major) * 100
            if major
            else None
        ),
        "backfill_measurable": backfill_measurable,
        "backfilled": sorted(backfilled, key=lambda b: -b[1]),
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
                "impact": i.get("impact"),
                "link": i["link"],
            }
            for i in sorted_inc
        ]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2)
