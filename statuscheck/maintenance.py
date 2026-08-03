"""Scheduled-maintenance analysis.

Statuspage exposes planned maintenance on a *separate* endpoint from incidents:

    /api/v2/scheduled-maintenances.json   recent maintenance windows, each with
                                          a planned start/end, an announcement
                                          time, staged updates (scheduled →
                                          in progress → completed), and
                                          affected-component tags

This is a distinct signal from incidents — it measures maintenance *discipline*:
how much advance notice customers get, whether windows land in low-impact hours,
how accurately planned windows predict actual duration, and how blast radius is
scoped (region-by-region vs global). Retrieval + all metrics are pure code, in
keeping with the code-vs-LLM split.
"""

import json
import re
from datetime import datetime
from statistics import mean, median

from .analysis import classify_component
from .net import FetchError, http_get

# AWS-style region tokens in window titles (us-east-1, eu-west-3, ap-south-1…)
REGION_RE = re.compile(
    r"\b(?:us|eu|ap|sa|ca|me|af)-(?:east|west|central|south|southeast|northeast|north)-\d\b",
    re.I,
)
# "…begins in 60 minutes", "…in 2 hours" — a pre-start countdown reminder
_REMINDER_RE = re.compile(r"\bin \d+\s?(?:minutes?|hours?|min|hrs?)\b", re.I)

_DAY = 1440  # minutes
_SANITY_CAP_MINUTES = 60 * 24 * 60  # ignore implausible spans (feed artifacts)


def _iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _minutes(a, b):
    """Signed minutes from a to b, or None if either is missing / implausible."""
    if not (a and b):
        return None
    delta = (b - a).total_seconds() / 60
    return delta if abs(delta) <= _SANITY_CAP_MINUTES else None


def parse_scheduled_maintenances(data):
    """Normalize a decoded scheduled-maintenances.json payload.

    Returns a list of records with parsed datetimes and derived timing
    (planned/actual duration, announcement lead time, overrun), the AWS
    regions named in the title, and communication signals. Only completed
    windows with a planned start are kept — cancelled or still-scheduled
    windows have no actual timing to measure.
    """
    records = []
    for m in data.get("scheduled_maintenances", []):
        if m.get("status") != "completed":
            continue
        sf, su = _iso(m.get("scheduled_for")), _iso(m.get("scheduled_until"))
        created = _iso(m.get("created_at"))
        resolved = _iso(m.get("resolved_at"))
        if not sf:
            continue
        ups = sorted(
            (u for u in (m.get("incident_updates") or []) if u.get("created_at")),
            key=lambda u: u["created_at"],
        )
        in_prog = next(
            (_iso(u["created_at"]) for u in ups if u.get("status") == "in_progress"),
            None,
        )
        completed = next(
            (_iso(u["created_at"]) for u in reversed(ups)
             if u.get("status") == "completed"),
            resolved,
        )
        has_reminder = any(
            u.get("status") == "scheduled" and _REMINDER_RE.search(u.get("body") or "")
            for u in ups
        )
        name = (m.get("name") or "").strip()
        records.append(
            {
                "name": name,
                "scheduled_for": sf,
                "scheduled_until": su,
                "created_at": created,
                "planned_minutes": _minutes(sf, su),
                "actual_minutes": _minutes(in_prog, completed),
                "lead_minutes": _minutes(created, sf),
                "overrun_minutes": _minutes(su, completed),
                "regions": sorted(set(r.group(0).lower() for r in REGION_RE.finditer(name))),
                "n_updates": len(ups),
                "has_reminder": has_reminder,
            }
        )
    return records


def fetch_scheduled_maintenances(page_url):
    """Fetch and normalize the page's scheduled-maintenance feed.

    Returns (raw_json_text, records). On any error or a page without the
    endpoint, returns (None, []).
    """
    url = page_url.rstrip("/") + "/api/v2/scheduled-maintenances.json"
    try:
        raw = http_get(url, retries=0)
        data = json.loads(raw)
    except (FetchError, json.JSONDecodeError):
        return None, []
    if not isinstance(data, dict) or "scheduled_maintenances" not in data:
        return None, []
    return raw, parse_scheduled_maintenances(data)


def analyze_maintenance(records, categories):
    """Compute maintenance-window metrics. Returns a dict, or None when fewer
    than 3 completed windows are available (too few to characterize)."""
    if len(records) < 3:
        return None

    recs = sorted(records, key=lambda r: r["scheduled_for"])
    n = len(recs)
    start, end = recs[0]["scheduled_for"], recs[-1]["scheduled_for"]
    span_days = max((end - start).days, 1)

    # ── cadence ──
    by_year, by_month = {}, {}
    for r in recs:
        y = r["scheduled_for"].year
        by_year[y] = by_year.get(y, 0) + 1
        mk = r["scheduled_for"].strftime("%Y-%m")
        by_month[mk] = by_month.get(mk, 0) + 1

    # ── advance notice ──
    leads = [r["lead_minutes"] for r in recs if r["lead_minutes"] is not None]
    short_notice = sorted(
        (r for r in recs if r["lead_minutes"] is not None and r["lead_minutes"] < _DAY),
        key=lambda r: r["lead_minutes"],
    )
    over_7d = sum(1 for l in leads if l >= 7 * _DAY)

    # ── duration: planned vs actual ──
    planned = [r["planned_minutes"] for r in recs if r["planned_minutes"] is not None]
    measurable = [
        r for r in recs
        if r["actual_minutes"] is not None and r["planned_minutes"] is not None
    ]
    within_plan = [r for r in measurable if r["actual_minutes"] <= r["planned_minutes"]]
    overruns = sorted(
        (r for r in recs if r["overrun_minutes"] is not None and r["overrun_minutes"] > 5),
        key=lambda r: -r["overrun_minutes"],
    )
    actual = [r["actual_minutes"] for r in measurable]

    # ── timing ──
    dow = {d: 0 for d in range(7)}
    hours = {h: 0 for h in range(24)}
    for r in recs:
        dow[r["scheduled_for"].weekday()] += 1
        hours[r["scheduled_for"].hour] += 1
    weekend = sum(1 for r in recs if r["scheduled_for"].weekday() >= 5)
    offhours = sum(1 for r in recs if 0 <= r["scheduled_for"].hour < 6)

    # ── services (config-driven, same classifier as incidents) & regions ──
    services = {}
    for r in recs:
        cat = classify_component(r["name"], "", categories)
        services[cat] = services.get(cat, 0) + 1
    regional = [r for r in recs if r["regions"]]
    regions = {}
    for r in regional:
        for reg in r["regions"]:
            regions[reg] = regions.get(reg, 0) + 1

    # ── communication hygiene ──
    staged = sum(1 for r in recs if r["n_updates"] >= 3)
    reminders = sum(1 for r in recs if r["has_reminder"])

    return {
        "count": n,
        "date_start": start.strftime("%Y-%m-%d"),
        "date_end": end.strftime("%Y-%m-%d"),
        "span_days": span_days,
        "per_year": n / (span_days / 365.25),
        "by_year": by_year,
        "by_month": by_month,
        "lead": {
            "median": median(leads) if leads else None,
            "mean": mean(leads) if leads else None,
            "min": min(leads) if leads else None,
            "max": max(leads) if leads else None,
            "under_24h": len(short_notice),
            "over_7d": over_7d,
            "short_notice": [
                (r["name"], r["lead_minutes"], r["scheduled_for"]) for r in short_notice
            ],
        },
        "duration": {
            "planned_median": median(planned) if planned else None,
            "planned_mean": mean(planned) if planned else None,
            "planned_max": max(planned) if planned else None,
            "actual_median": median(actual) if actual else None,
            "actual_mean": mean(actual) if actual else None,
            "actual_max": max(actual) if actual else None,
            "measurable": len(measurable),
            "within_plan": len(within_plan),
            "overran": len(overruns),
            "overruns": [
                (r["name"], r["overrun_minutes"], r["planned_minutes"], r["scheduled_for"])
                for r in overruns[:6]
            ],
            "longest_planned": [
                (r["name"], r["planned_minutes"], r["scheduled_for"])
                for r in sorted(
                    (r for r in recs if r["planned_minutes"] is not None),
                    key=lambda r: -r["planned_minutes"],
                )[:5]
            ],
        },
        "timing": {
            "offhours": offhours,
            "weekend": weekend,
            "dow": dow,
            "hours": hours,
        },
        "services": sorted(services.items(), key=lambda kv: -kv[1]),
        "regions": {
            "regional_count": len(regional),
            "counts": sorted(regions.items(), key=lambda kv: -kv[1]),
        },
        "comms": {
            "staged": staged,
            "reminders": reminders,
            "avg_updates": mean([r["n_updates"] for r in recs]),
        },
    }
