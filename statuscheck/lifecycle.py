"""Incident lifecycle metrics: durations, resolution speed, update gaps.

Timing data comes from the Statuspage JSON API (created_at/resolved_at and
per-update timestamps) or, best-effort, from the timestamps embedded in
Statuspage Atom feed descriptions. incident.io-style feeds carry only the
final update per incident, so no lifecycle metrics are possible there —
analyze_lifecycle returns None and the report says so.
"""

from statistics import median

from .parser import extract_timed_updates

RESOLVED_STATUSES = {"resolved", "completed", "complete", "postmortem"}

# (label, upper bound in minutes)
DURATION_BUCKETS = [
    ("< 30 min", 30),
    ("30 min – 2 h", 120),
    ("2 – 8 h", 480),
    ("8 – 24 h", 1440),
    ("> 24 h", float("inf")),
]

# Ignore implausible values (feed artifacts, backdated edits)
_SANITY_CAP_MINUTES = 60 * 24 * 30


def fmt_minutes(minutes):
    """Format a minute count as a human duration: 47 min, 3h 05m, 2d 4h."""
    m = int(round(minutes))
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h}h {mm:02d}m"
    d, hh = divmod(h, 24)
    return f"{d}d {hh}h"


def _timed_updates(incident):
    return incident.get("updates_timed") or extract_timed_updates(
        incident["desc_raw"], incident.get("pub_date")
    )


def analyze_lifecycle(incidents):
    """Compute duration and update-gap metrics. Returns a dict, or None
    when fewer than 3 incidents have usable timing data."""
    durations = []
    max_gaps = []
    for i in incidents:
        ups = _timed_updates(i)

        start = i.get("created_at") or (ups[0]["at"] if ups else None)
        end = i.get("resolved_at")
        if end is None and len(ups) >= 2 and ups[-1]["status"].lower() in RESOLVED_STATUSES:
            end = ups[-1]["at"]
        if start and end:
            minutes = (end - start).total_seconds() / 60
            if 0 <= minutes <= _SANITY_CAP_MINUTES:
                durations.append(
                    {
                        "minutes": minutes,
                        "title": i["title"],
                        "date": i["pub_date"].strftime("%Y-%m-%d") if i["pub_date"] else "?",
                    }
                )

        if len(ups) >= 2:
            gaps = [
                (ups[k]["at"] - ups[k - 1]["at"]).total_seconds() / 60
                for k in range(1, len(ups))
            ]
            worst = max(gaps)
            if 0 <= worst <= _SANITY_CAP_MINUTES:
                max_gaps.append(worst)

    if len(durations) < 3:
        return None

    minutes_sorted = sorted(d["minutes"] for d in durations)
    n = len(minutes_sorted)

    buckets = {label: 0 for label, _ in DURATION_BUCKETS}
    for m in minutes_sorted:
        for label, ceiling in DURATION_BUCKETS:
            if m <= ceiling:
                buckets[label] += 1
                break

    return {
        "covered": n,
        "total": len(incidents),
        "median_minutes": median(minutes_sorted),
        "p90_minutes": minutes_sorted[int(0.9 * (n - 1))],
        "max_minutes": minutes_sorted[-1],
        "buckets": buckets,
        "within": {
            "1h": sum(1 for m in minutes_sorted if m <= 60) / n * 100,
            "4h": sum(1 for m in minutes_sorted if m <= 240) / n * 100,
            "24h": sum(1 for m in minutes_sorted if m <= 1440) / n * 100,
        },
        "longest": sorted(durations, key=lambda d: -d["minutes"])[:5],
        "gap_covered": len(max_gaps),
        "gap_median_minutes": median(max_gaps) if max_gaps else None,
        "gap_worst_minutes": max(max_gaps) if max_gaps else None,
    }
