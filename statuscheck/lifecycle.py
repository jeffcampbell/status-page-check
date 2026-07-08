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


def duration_minutes(incident, ups=None):
    """Duration of one incident in minutes, or None if not computable."""
    if ups is None:
        ups = _timed_updates(incident)
    start = incident.get("created_at") or (ups[0]["at"] if ups else None)
    end = incident.get("resolved_at")
    if end is None and len(ups) >= 2 and ups[-1]["status"].lower() in RESOLVED_STATUSES:
        end = ups[-1]["at"]
    if not (start and end):
        return None
    minutes = (end - start).total_seconds() / 60
    if not 0 <= minutes <= _SANITY_CAP_MINUTES:
        return None
    return minutes


def analyze_lifecycle(incidents):
    """Compute duration and update-gap metrics. Returns a dict, or None
    when fewer than 3 incidents have usable timing data."""
    durations = []
    max_gaps = []
    for i in incidents:
        ups = _timed_updates(i)

        minutes = duration_minutes(i, ups)
        if minutes is not None:
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


def disclosed_downtime(incidents, min_window_days=30):
    """Estimate total disclosed downtime and implied availability.

    Sums durations of major/critical incidents when severity data exists
    (falling back to all duration-computable incidents otherwise) over the
    window where that data is available. This measures what the company
    *publishes*, not true availability — the report must carry that caveat.

    Returns None when there's no usable timing data or the window is too
    short to be meaningful.
    """
    timed = []
    for i in incidents:
        minutes = duration_minutes(i)
        if minutes is not None:
            timed.append((i, minutes))
    if not timed:
        return None

    with_severity = [(i, m) for i, m in timed if i.get("impact")]
    if with_severity:
        pool = [(i, m) for i, m in with_severity if i["impact"] in ("critical", "major")]
        window_set = with_severity  # availability window = where severity is known
        basis = "major/critical incidents"
    else:
        pool = timed
        window_set = timed
        basis = "all incidents with computable duration"

    dates = sorted(i["pub_date"] for i, _ in window_set if i["pub_date"])
    if not dates:
        return None
    window_days = (dates[-1] - dates[0]).days
    if window_days < min_window_days:
        return None

    total_minutes = sum(m for _, m in pool)
    return {
        "basis": basis,
        "incident_count": len(pool),
        "total_minutes": total_minutes,
        "window_days": window_days,
        "availability_pct": (1 - total_minutes / (window_days * 1440)) * 100,
    }
