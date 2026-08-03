"""Markdown report generation.

Renders the full assessment report from computed data. LLM-generated
sections (executive summary, tone assessment, refined recommendations,
templates) are inserted when available; every section has a deterministic
fallback so the report is complete without any LLM.
"""

from datetime import datetime, timezone

from . import REPO_URL
from .lifecycle import DURATION_BUCKETS, fmt_minutes

DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

SEVERITY_ORDER = ["critical", "major", "minor", "none", "maintenance"]

MODE_TITLES = {
    "neutral": "Incident Management — External Assessment",
    "self": "Incident Communications — Internal Audit",
    "vendor": "Vendor Reliability & Transparency Assessment",
}

MODE_SECTION3 = {
    "neutral": "Recommendations",
    "self": "Recommendations",
    "vendor": "Risk Assessment & Questions for the Vendor",
}


def _bar(count, max_count, width=40):
    if max_count <= 0:
        return ""
    return "█" * max(1, round(count / max_count * width)) if count else ""


def _table(headers, rows):
    def cell(c):
        return str(c).replace("|", "\\|")

    lines = [
        "| " + " | ".join(cell(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell(c) for c in row) + " |")
    return lines


def rule_based_recommendations(messaging, cadence, comparison=None, lifecycle=None):
    """Generate recommendations from data thresholds. Pure code, no LLM."""
    recs = []
    total = messaging["count"]

    structure = {s["check"]: s["pct"] for s in messaging["structure"]}
    if structure.get("Support link included", 100) < 80:
        recs.append(
            f"Standardize a support-link footer: only {structure['Support link included']:.0f}% "
            "of incidents include a support link."
        )
    if structure.get("Root cause mentioned", 100) < 40:
        recs.append(
            f"Add a brief root-cause statement to resolved messages: currently present in "
            f"{structure['Root cause mentioned']:.0f}% of incidents."
        )
    if structure.get("Incident timeline included", 100) < 40:
        recs.append(
            f"Include the impact window (start/end times, UTC) in resolutions: currently "
            f"{structure['Incident timeline included']:.0f}%."
        )
    if structure.get("Customer action steps", 100) < 40:
        recs.append(
            f"State customer next steps (replay, reconnect, refresh) where applicable: "
            f"currently {structure['Customer action steps']:.0f}%."
        )

    minimal = len(messaging["minimal_incidents"])
    if minimal / total > 0.10:
        recs.append(
            f"Set a minimum content standard for resolutions: {minimal}/{total} incidents "
            "resolve with under 100 characters of detail."
        )

    named_openers = [k for k in messaging["openers"] if not k.startswith("Other:")]
    other_openers = sum(
        v for k, v in messaging["openers"].items() if k.startswith("Other:")
    )
    if len(named_openers) + other_openers > 5:
        recs.append(
            "Adopt status-update templates: resolution messages open in "
            f"{len(named_openers) + other_openers} distinct patterns, indicating no shared template."
        )

    if len(messaging["link_variants"]) > 2:
        recs.append(
            f"Consolidate support URLs: {len(messaging['link_variants'])} different "
            "support-link variants appear across incidents."
        )

    if messaging["quality_issues"]:
        recs.append(
            f"Add a review/spellcheck step before publishing: {len(messaging['quality_issues'])} "
            "quality issues (typos, repeated words, terse messages) detected."
        )

    if cadence["round_minute_pct"] > 15:
        recs.append(
            f"Posting times cluster on :00/:15/:30/:45 ({cadence['round_minute_pct']:.0f}%), "
            "suggesting scheduled or batched posting — verify updates go out when the "
            "incident state actually changes."
        )

    tight_clusters = [c for c in cadence["clusters"] if c["min_gap_minutes"] < 5]
    if tight_clusters:
        recs.append(
            f"Review incident-splitting hygiene: {len(tight_clusters)} days have multiple "
            "incidents posted within 5 minutes of each other, suggesting one root cause "
            "split into several posts."
        )

    if comparison and comparison.get("rate_change_pct", 0) > 25:
        recs.append(
            f"Incident rate rose {comparison['rate_change_pct']:.0f}% between periods "
            f"({comparison['older_per_week']:.1f} → {comparison['newer_per_week']:.1f}/week) — "
            "worth a capacity and release-process review."
        )

    if lifecycle:
        gap = lifecycle.get("gap_median_minutes")
        if gap and gap > 60:
            recs.append(
                f"Commit to a public update cadence: during open incidents the median "
                f"longest silence between updates is {fmt_minutes(gap)} "
                f"(worst: {fmt_minutes(lifecycle['gap_worst_minutes'])}). A stated "
                "'next update by' time keeps customers from refreshing in the dark."
            )
        if lifecycle["p90_minutes"] > 1440:
            recs.append(
                f"Review escalation for long-running incidents: 10% of incidents run "
                f"longer than {fmt_minutes(lifecycle['p90_minutes'])} (max "
                f"{fmt_minutes(lifecycle['max_minutes'])})."
            )

    return recs


def rule_based_risk_notes(
    messaging, cadence, comparison=None, lifecycle=None,
    transparency=None, downtime=None,
):
    """Vendor-mode counterpart to rule_based_recommendations: risk signals
    addressed to a team evaluating this company as a dependency."""
    notes = []

    if comparison and comparison.get("rate_change_pct", 0) > 25:
        notes.append(
            f"The incident rate is rising: {comparison['rate_change_pct']:.0f}% "
            f"increase between periods ({comparison['older_per_week']:.1f} → "
            f"{comparison['newer_per_week']:.1f}/week)."
        )

    if downtime:
        notes.append(
            f"Disclosed downtime: {downtime['incident_count']} {downtime['basis']} "
            f"totaled {fmt_minutes(downtime['total_minutes'])} over "
            f"{downtime['window_days']} days (~{downtime['availability_pct']:.2f}% "
            "implied disclosed availability). Actual availability may be lower — "
            "this counts only what the company published."
        )

    if lifecycle:
        if lifecycle["p90_minutes"] > 480:
            notes.append(
                f"Long-tail risk: 10% of incidents run longer than "
                f"{fmt_minutes(lifecycle['p90_minutes'])} "
                f"(worst: {fmt_minutes(lifecycle['max_minutes'])})."
            )
        gap = lifecycle.get("gap_median_minutes")
        if gap and gap > 60:
            notes.append(
                f"Expect slow communication during incidents: the median longest "
                f"silence between updates is {fmt_minutes(gap)}."
            )

    if transparency:
        if transparency["never_above_minor"]:
            notes.append(
                "Severity skepticism warranted: despite declaring severity on "
                f"{transparency['severity_coverage_pct']:.0f}% of incidents, this "
                "page has never rated one above 'minor'."
            )
        if transparency["postmortem_rate_pct"] < 30:
            notes.append(
                f"Limited public accountability: only "
                f"{transparency['postmortem_rate_pct']:.0f}% of "
                f"{transparency['postmortem_basis']} reference a postmortem or "
                "root-cause analysis."
            )
        if transparency["backfilled"]:
            notes.append(
                f"Late disclosure: {len(transparency['backfilled'])} incident(s) "
                "were published 24+ hours after their declared start time."
            )

    structure = {s["check"]: s["pct"] for s in messaging["structure"]}
    thin = [
        name for name in ("Root cause mentioned", "Incident timeline included")
        if structure.get(name, 100) < 40
    ]
    if thin:
        notes.append(
            "Resolution messages are often thin on specifics ("
            + "; ".join(f"{name.lower()}: {structure[name]:.0f}%" for name in thin)
            + ") — you may struggle to assess impact during their incidents."
        )

    return notes


def build_comparison(stats_older, stats_newer):
    """Compute period-over-period deltas as a plain dict."""
    a, b = stats_older, stats_newer
    rate_change = (
        ((b["per_week"] - a["per_week"]) / a["per_week"] * 100) if a["per_week"] else 0
    )
    return {
        "older_label": a["label"],
        "newer_label": b["label"],
        "older_count": a["count"],
        "newer_count": b["count"],
        "older_per_week": a["per_week"],
        "newer_per_week": b["per_week"],
        "rate_change_pct": rate_change,
        "new_categories": sorted(set(b["components"]) - set(a["components"])),
        "gone_categories": sorted(set(a["components"]) - set(b["components"])),
    }


def _pct(n, d):
    return f"{round(100 * n / d)}%" if d else "0%"


def render_maintenance(maint):
    """Render the scheduled-maintenance section as markdown lines.

    Factual and mode-agnostic (like transparency signals) — it renders in
    every mode whenever the page exposes a scheduled-maintenance feed.
    """
    n = maint["count"]
    L = ["## Scheduled Maintenance", ""]
    L.append(
        "*Planned windows from the page's scheduled-maintenance feed "
        "(separate from the incidents feed). Measures maintenance discipline: "
        "advance notice, low-impact timing, planned-vs-actual accuracy, and "
        "blast-radius scoping.*"
    )
    L.append("")
    L.append(
        f"**{n} completed windows** from {maint['date_start']} to "
        f"{maint['date_end']} (~{maint['per_year']:.0f}/year). Metrics below are "
        "computed deterministically from the feed."
    )
    L.append("")

    # ── Cadence ──
    L.append("### Cadence")
    L.append("")
    L.extend(_table(["Year", "Windows"], sorted(maint["by_year"].items())))
    L.append("")

    # ── Advance notice ──
    lead = maint["lead"]
    L.append("### Advance Notice")
    L.append("")
    L.append(
        "How far ahead of the window each maintenance was first announced "
        "(planned start − announcement time):"
    )
    L.append("")
    if lead["median"] is not None:
        L.extend(
            _table(
                ["Metric", "Value"],
                [
                    ("Median lead time", fmt_minutes(lead["median"])),
                    ("Mean lead time", fmt_minutes(lead["mean"])),
                    ("Shortest", fmt_minutes(max(lead["min"], 0))),
                    ("Longest", fmt_minutes(lead["max"])),
                    ("Announced < 24 h ahead", f"{lead['under_24h']}/{n} ({_pct(lead['under_24h'], n)})"),
                    ("Announced ≥ 7 days ahead", f"{lead['over_7d']}/{n} ({_pct(lead['over_7d'], n)})"),
                ],
            )
        )
    if lead["short_notice"]:
        L.append("")
        L.append("Short-notice windows (< 24 h warning):")
        L.append("")
        for name, lead_min, sf in lead["short_notice"][:8]:
            L.append(
                f"- **{fmt_minutes(max(lead_min, 0))}** notice — {name} "
                f"({sf:%Y-%m-%d %H:%M} UTC)"
            )
    L.append("")

    # ── Duration ──
    d = maint["duration"]
    L.append("### Window Duration — Planned vs Actual")
    L.append("")
    if d["planned_median"] is not None and d["actual_median"] is not None:
        L.extend(
            _table(
                ["Metric", "Planned", "Actual"],
                [
                    ("Median", fmt_minutes(d["planned_median"]), fmt_minutes(d["actual_median"])),
                    ("Mean", fmt_minutes(d["planned_mean"]), fmt_minutes(d["actual_mean"])),
                    ("Longest", fmt_minutes(d["planned_max"]), fmt_minutes(d["actual_max"])),
                ],
            )
        )
        L.append("")
        L.append(
            f"- **Finished within the planned window:** {d['within_plan']}/{d['measurable']} "
            f"({_pct(d['within_plan'], d['measurable'])}) of windows with measurable timing."
        )
        L.append(
            f"- **Ran past the planned end (>5 min):** {d['overran']}/{d['measurable']} "
            f"({_pct(d['overran'], d['measurable'])})."
        )
    if d["overruns"]:
        L.append("")
        L.append("Largest overruns (completion past planned end):")
        L.append("")
        for name, over, planned, sf in d["overruns"]:
            L.append(
                f"- **+{fmt_minutes(over)}** past plan — {name} "
                f"(planned {fmt_minutes(planned)}, {sf:%Y-%m-%d})"
            )
    if d["longest_planned"]:
        L.append("")
        L.append("Longest planned windows:")
        L.append("")
        for name, planned, sf in d["longest_planned"]:
            L.append(f"- **{fmt_minutes(planned)}** — {name} ({sf:%Y-%m-%d})")
    L.append("")

    # ── Timing ──
    t = maint["timing"]
    L.append("### Timing")
    L.append("")
    L.append(
        f"- **Off-hours (00:00–06:00 UTC) starts:** {t['offhours']}/{n} "
        f"({_pct(t['offhours'], n)})."
    )
    L.append(
        f"- **Weekend starts (Sat/Sun):** {t['weekend']}/{n} "
        f"({_pct(t['weekend'], n)})."
    )
    L.append("")
    L.append("Window start hour (UTC):")
    L.append("")
    L.append("```")
    max_h = max(t["hours"].values(), default=0)
    for h in range(24):
        c = t["hours"].get(h, 0)
        L.append(f"{h:02d}:00  {c:>3}  {_bar(c, max_h, 30)}")
    L.append("```")
    L.append("")

    # ── Services ──
    L.append("### Affected Services")
    L.append("")
    L.extend(_table(["Service (from window title)", "Windows"], maint["services"]))
    L.append("")

    # ── Regional rollout ──
    reg = maint["regions"]
    if reg["regional_count"]:
        L.append("### Regional Rollout")
        L.append("")
        L.append(
            f"{reg['regional_count']}/{n} windows target a specific region — "
            "region-scoped maintenance is rolled out one region at a time rather "
            "than globally."
        )
        L.append("")
        if reg["counts"]:
            L.extend(_table(["Region", "Windows"], reg["counts"]))
            L.append("")

    # ── Communication ──
    c = maint["comms"]
    L.append("### Communication During Windows")
    L.append("")
    L.append(
        f"- **Staged updates (≥3 posts: scheduled → in progress → completed):** "
        f"{c['staged']}/{n} ({_pct(c['staged'], n)})."
    )
    L.append(
        f"- **Posted a countdown reminder** (e.g. \"begins in 60 minutes\"): "
        f"{c['reminders']}/{n} ({_pct(c['reminders'], n)})."
    )
    L.append(f"- Average updates per window: **{c['avg_updates']:.1f}**.")
    L.append("")
    return L


def _fallback_summary(company, stats, messaging, comparison):
    top = stats["components"].most_common(1)
    top_txt = f" The most affected area is {top[0][0]} ({top[0][1]} incidents)." if top else ""
    cmp_txt = ""
    if comparison:
        direction = "rose" if comparison["rate_change_pct"] > 0 else "fell"
        cmp_txt = (
            f" The incident rate {direction} {abs(comparison['rate_change_pct']):.0f}% "
            f"between {comparison['older_label']} and {comparison['newer_label']}."
        )
    return (
        f"{company} published {stats['count']} incidents between {stats['date_start']} "
        f"and {stats['date_end']} ({stats['per_week']:.1f} per week).{top_txt}{cmp_txt} "
        f"Resolution messages average {messaging['lengths']['avg']} characters, and "
        f"{len(messaging['quality_issues'])} messaging quality issues were detected. "
        "Detailed findings follow."
    )


def render_report(
    company,
    stats_all,
    periods,          # list of per-period stats dicts (1 or 2, oldest first)
    messaging,        # messaging analysis over all incidents
    period_messaging, # list matching `periods`
    cadence,
    llm_sections,     # dict or {}
    meta,             # source_label, snapshots_used, llm_label, focus_terms
    lifecycle=None,   # from analyze_lifecycle, or None
    transparency=None,  # from analyze_transparency, or None
    downtime=None,    # from disclosed_downtime, or None
    mode="neutral",   # neutral | self | vendor
    overall_stats=None,  # unfocused stats for context when --focus is used
    maintenance=None,   # from analyze_maintenance, or None
):
    comparison = build_comparison(periods[0], periods[1]) if len(periods) == 2 else None
    if mode == "vendor":
        recs = rule_based_risk_notes(
            messaging, cadence, comparison, lifecycle, transparency, downtime
        )
    else:
        recs = rule_based_recommendations(messaging, cadence, comparison, lifecycle)
    llm = llm_sections or {}
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    L = []
    L.append(f"# {company}: {MODE_TITLES.get(mode, MODE_TITLES['neutral'])}")
    L.append("")
    L.append(
        f"*Generated {generated} by [status-page-check]({REPO_URL}) "
        "from public status page data.*"
    )
    L.append("")
    if meta.get("focus_terms"):
        L.append(
            f"> **Focus:** analysis restricted to incidents matching "
            f"*{meta['focus_terms']}* — {stats_all['count']} of "
            f"{overall_stats['count'] if overall_stats else '?'} total incidents. "
            "Overall volume is shown for context in §1.1."
        )
        L.append("")

    # ── Executive summary ──
    L.append("## Executive Summary")
    L.append("")
    L.append(llm.get("executive_summary") or _fallback_summary(company, stats_all, messaging, comparison))
    L.append("")

    # ── 1. Frequency & trends ──
    L.append("## 1. Incident Frequency & Trends")
    L.append("")
    L.append("### 1.1 Overall Volume")
    L.append("")
    rows = [
        (
            p["label"],
            f"{p['date_start']} → {p['date_end']}",
            p["count"],
            f"{p['per_week']:.1f}",
        )
        for p in ([stats_all] if len(periods) < 2 else periods)
    ]
    if overall_stats:
        rows.append(
            (
                "All incidents (context)",
                f"{overall_stats['date_start']} → {overall_stats['date_end']}",
                overall_stats["count"],
                f"{overall_stats['per_week']:.1f}",
            )
        )
    L.extend(_table(["Period", "Dates", "Incidents", "Per week"], rows))
    if comparison:
        arrow = "▲" if comparison["rate_change_pct"] > 0 else "▼"
        L.append("")
        L.append(
            f"Rate change between periods: {arrow} "
            f"**{abs(comparison['rate_change_pct']):.0f}%**"
        )
    if stats_all.get("severity"):
        sev = stats_all["severity"]
        L.append("")
        L.append("Declared severity (the page's own impact levels):")
        L.append("")
        ordered = [s for s in SEVERITY_ORDER if s in sev] + [
            s for s in sev if s not in SEVERITY_ORDER
        ]
        L.extend(
            _table(
                ["Impact", "Incidents", "%"],
                [
                    (s, sev[s], f"{sev[s] / stats_all['count'] * 100:.0f}%")
                    for s in ordered
                ],
            )
        )
    L.append("")

    L.append("### 1.2 Monthly Distribution")
    L.append("")
    L.append("```")
    max_m = max(stats_all["monthly"].values(), default=0)
    for month in sorted(stats_all["monthly"]):
        c = stats_all["monthly"][month]
        L.append(f"{month}  {c:>3}  {_bar(c, max_m)}")
    L.append("```")
    L.append("")

    L.append("### 1.3 Affected Components / Areas")
    L.append("")
    if comparison:
        a, b = periods
        cats = sorted(
            set(a["components"]) | set(b["components"]),
            key=lambda c: -(a["components"].get(c, 0) + b["components"].get(c, 0)),
        )
        rows = []
        for cat in cats:
            ca, cb = a["components"].get(cat, 0), b["components"].get(cat, 0)
            delta = cb - ca
            rows.append((cat, ca, cb, f"{'+' if delta > 0 else ''}{delta}"))
        L.extend(_table(["Category", a["label"], b["label"], "Δ"], rows))
        if comparison["new_categories"]:
            L.append("")
            L.append(f"New in {b['label']}: " + ", ".join(comparison["new_categories"]))
        if comparison["gone_categories"]:
            L.append("")
            L.append(f"Not seen in {b['label']}: " + ", ".join(comparison["gone_categories"]))
    else:
        rows = [(cat, c) for cat, c in stats_all["components"].most_common()]
        L.extend(_table(["Category", "Incidents"], rows))
    L.append("")
    if stats_all["affected"]:
        L.append("Most frequently listed status page components:")
        L.append("")
        L.extend(
            _table(
                ["Component", "Times listed"],
                stats_all["affected"].most_common(12),
            )
        )
        L.append("")

    L.append("### 1.4 Failure Modes (keywords in titles)")
    L.append("")
    if comparison:
        a, b = periods
        kws = sorted(
            set(a["keywords"]) | set(b["keywords"]),
            key=lambda k: -(a["keywords"].get(k, 0) + b["keywords"].get(k, 0)),
        )
        rows = []
        for kw in kws:
            ka, kb = a["keywords"].get(kw, 0), b["keywords"].get(kw, 0)
            delta = kb - ka
            rows.append((kw, ka, kb, f"{'+' if delta > 0 else ''}{delta}"))
        L.extend(_table(["Keyword", a["label"], b["label"], "Δ"], rows))
    else:
        L.extend(_table(["Keyword", "Count"], stats_all["keywords"].most_common()))
    L.append("")

    L.append("### 1.5 Day-of-Week Patterns")
    L.append("")
    if comparison:
        a, b = periods
        rows = [
            (d, a["dow"].get(d, 0), b["dow"].get(d, 0)) for d in DOW_ORDER
        ]
        L.extend(_table(["Day", a["label"], b["label"]], rows))
    else:
        L.append("```")
        max_d = max(stats_all["dow"].values(), default=0)
        for d in DOW_ORDER:
            c = stats_all["dow"].get(d, 0)
            L.append(f"{d:<10} {c:>3}  {_bar(c, max_d)}")
        L.append("```")
    L.append("")

    L.append("### 1.6 Same-Day Clustering")
    L.append("")
    if cadence["clusters"]:
        L.append(
            f"{len(cadence['clusters'])} days had multiple incidents. Days with "
            "posts under 5 minutes apart (possible batch posting or a split root cause):"
        )
        L.append("")
        tight = [c for c in cadence["clusters"] if c["min_gap_minutes"] < 5]
        if tight:
            for c in tight:
                L.append(f"- **{c['date']}** (min gap {c['min_gap_minutes']:.0f} min):")
                for t, title in c["incidents"]:
                    L.append(f"  - {t} UTC — {title}")
        else:
            L.append("None — same-day incidents were spaced apart.")
    else:
        L.append("No days with multiple incidents.")
    L.append("")

    L.append("### 1.7 Incident Duration & Lifecycle")
    L.append("")
    if lifecycle:
        L.append(
            f"Duration computable for {lifecycle['covered']}/{lifecycle['total']} "
            "incidents (those with usable start/resolve timing)."
        )
        L.append("")
        L.extend(
            _table(
                ["Metric", "Value"],
                [
                    ("Median time to resolve", fmt_minutes(lifecycle["median_minutes"])),
                    ("90th percentile", fmt_minutes(lifecycle["p90_minutes"])),
                    ("Longest incident", fmt_minutes(lifecycle["max_minutes"])),
                    ("Resolved within 1 h", f"{lifecycle['within']['1h']:.0f}%"),
                    ("Resolved within 4 h", f"{lifecycle['within']['4h']:.0f}%"),
                    ("Resolved within 24 h", f"{lifecycle['within']['24h']:.0f}%"),
                ],
            )
        )
        L.append("")
        L.append("```")
        max_b = max(lifecycle["buckets"].values(), default=0)
        for label, _ in DURATION_BUCKETS:
            c = lifecycle["buckets"][label]
            L.append(f"{label:<14} {c:>3}  {_bar(c, max_b)}")
        L.append("```")
        L.append("")
        L.append("Longest incidents:")
        L.append("")
        for d in lifecycle["longest"]:
            L.append(f"- **{fmt_minutes(d['minutes'])}** — {d['title']} ({d['date']})")
        if lifecycle["gap_median_minutes"] is not None:
            L.append("")
            L.append(
                f"Update cadence during incidents ({lifecycle['gap_covered']} incidents "
                f"with 2+ updates): the median longest silence between consecutive "
                f"updates is **{fmt_minutes(lifecycle['gap_median_minutes'])}** "
                f"(worst: {fmt_minutes(lifecycle['gap_worst_minutes'])})."
            )
    else:
        L.append(
            "No per-update timing data is available from this source — the feed "
            "carries only final updates per incident, so durations can't be computed. "
            "(Statuspage-hosted pages expose timing via their JSON API and Atom feeds.)"
        )
    if downtime:
        L.append("")
        L.append(
            f"**Disclosed downtime:** {downtime['incident_count']} "
            f"{downtime['basis']} totaled "
            f"**{fmt_minutes(downtime['total_minutes'])}** of disclosed "
            f"degradation over a {downtime['window_days']}-day window — an "
            f"implied disclosed availability of "
            f"**{downtime['availability_pct']:.2f}%**."
        )
        L.append("")
        L.append(
            "> This measures only what the company published: unreported or "
            "under-scoped incidents aren't counted, and severity is "
            "self-declared. Treat it as a floor on downtime, not a "
            "measurement of availability."
        )
    L.append("")

    # ── 2. Messaging analysis ──
    L.append("## 2. Status Page Messaging Analysis")
    L.append("")
    if messaging["has_timeline"]:
        u = messaging["updates_per_incident"]
        L.append(
            f"The source includes the full update timeline per incident "
            f"(min {u['min']}, max {u['max']}, avg {u['avg']:.1f} updates/incident)."
        )
    else:
        L.append(
            "> **Caveat:** this source contains only the *final* update per incident "
            "(usually the Resolved message). This section analyzes resolution "
            "communications, not the full incident lifecycle."
        )
    L.append("")

    L.append("### 2.1 Structure & Completeness")
    L.append("")
    rows = [
        (s["check"], f"{s['count']}/{messaging['count']}", f"{s['pct']:.0f}%")
        for s in messaging["structure"]
    ]
    L.extend(_table(["Element", "Incidents", "%"], rows))
    L.append("")
    if comparison and len(period_messaging) == 2 and all(period_messaging):
        ma, mb = period_messaging
        L.append("Change between periods:")
        L.append("")
        pa = {s["check"]: s["pct"] for s in ma["structure"]}
        pb = {s["check"]: s["pct"] for s in mb["structure"]}
        rows = [
            (check, f"{pa.get(check, 0):.0f}%", f"{pb.get(check, 0):.0f}%")
            for check in pa
        ]
        rows.append(
            ("Average message length", f"{ma['lengths']['avg']} chars", f"{mb['lengths']['avg']} chars")
        )
        L.extend(_table(["Metric", periods[0]["label"], periods[1]["label"]], rows))
        L.append("")

    L.append("### 2.2 Detail Level")
    L.append("")
    lg = messaging["lengths"]
    L.append(
        f"Message length: min {lg['min']}, median {lg['median']}, "
        f"average {lg['avg']}, max {lg['max']} characters."
    )
    L.append("")
    rows = [
        (bucket, count, f"{count / messaging['count'] * 100:.0f}%")
        for bucket, count in messaging["length_buckets"].items()
    ]
    L.extend(_table(["Bucket", "Incidents", "%"], rows))
    if messaging["minimal_incidents"]:
        # Internal audits get the full list; external reports stay compact
        limit = len(messaging["minimal_incidents"]) if mode == "self" else 10
        L.append("")
        L.append("Minimally-detailed incidents (< 100 chars):")
        L.append("")
        for title, text in messaging["minimal_incidents"][:limit]:
            L.append(f'- **{title}** — "{text}"')
        extra = len(messaging["minimal_incidents"]) - limit
        if extra > 0:
            L.append(f"- *…and {extra} more*")
    L.append("")

    L.append("### 2.3 Tone & Voice")
    L.append("")
    L.append("How resolution messages open:")
    L.append("")
    L.extend(_table(["Opening pattern", "Count"], messaging["openers"].most_common(12)))
    L.append("")
    if llm.get("tone_assessment"):
        L.append(llm["tone_assessment"])
        L.append("")
    else:
        named = [k for k in messaging["openers"] if not k.startswith("Other:")]
        L.append(
            f"Resolution messages open with {len(messaging['openers'])} distinct "
            f"patterns ({len(named)} recognizable buckets). High variety suggests "
            "no shared template. Run with an LLM provider configured for a "
            "qualitative tone and sentiment assessment."
        )
        L.append("")

    L.append("### 2.4 Support Link Consistency")
    L.append("")
    if messaging["link_variants"]:
        L.extend(_table(["Support link variant", "Count"], messaging["link_variants"].most_common()))
    else:
        L.append("No support links detected in any incident.")
    if messaging["no_support_link"]:
        L.append("")
        L.append(
            f"{len(messaging['no_support_link'])}/{messaging['count']} incidents "
            "contain no support link."
        )
    L.append("")

    L.append("### 2.5 Quality Issues")
    L.append("")
    if messaging["quality_issues"]:
        for title, issue in messaging["quality_issues"]:
            L.append(f"- [{issue}] {title}")
    else:
        L.append("No typos, repeated words, or terse-message issues detected.")
    L.append("")

    L.append("### 2.6 Posting Cadence & Automation Signals")
    L.append("")
    L.append(
        f"Posts at :00/:15/:30/:45 minutes: {cadence['round_minute_count']}/"
        f"{cadence['dated_count']} ({cadence['round_minute_pct']:.0f}%). "
        "Above ~15% suggests scheduled or automated posting; near the random "
        "baseline (~7%) suggests manual posting."
    )
    L.append("")
    L.append("Hour-of-day distribution (UTC) — shows operational coverage hours:")
    L.append("")
    L.append("```")
    max_h = max(cadence["hours"].values(), default=0)
    for h in range(24):
        c = cadence["hours"].get(h, 0)
        L.append(f"{h:02d}:00  {c:>3}  {_bar(c, max_h)}")
    L.append("```")
    L.append("")

    if transparency:
        L.append("### 2.7 Transparency Signals")
        L.append("")
        rows = [
            (
                "Severity declared",
                f"{transparency['severity_coverage_pct']:.0f}% of incidents",
            ),
            ("Major/critical incidents declared", transparency["major_count"]),
            (
                f"Postmortem / RCA referenced ({transparency['postmortem_basis']})",
                f"{transparency['postmortem_rate_pct']:.0f}%",
            ),
        ]
        if transparency["root_cause_major_pct"] is not None:
            rows.append(
                (
                    "Root cause mentioned (major/critical)",
                    f"{transparency['root_cause_major_pct']:.0f}%",
                )
            )
        if transparency["backfill_measurable"]:
            rows.append(
                (
                    "Disclosed 24h+ after declared start",
                    f"{len(transparency['backfilled'])} of "
                    f"{transparency['backfill_measurable']} measurable",
                )
            )
        L.extend(_table(["Signal", "Value"], rows))
        if transparency["never_above_minor"]:
            L.append("")
            L.append(
                "⚠ This page declares severity but has **never rated an incident "
                "above 'minor'** in the analyzed window — declared severity may "
                "understate impact."
            )
        if transparency["backfilled"]:
            L.append("")
            L.append("Incidents disclosed 24h+ after their declared start:")
            L.append("")
            for title, delay_h in transparency["backfilled"][:5]:
                L.append(f"- {title} — {fmt_minutes(delay_h * 60)} late")
        L.append("")

    if mode == "self" and messaging.get("shortest_messages"):
        L.append("### 2.8 Exhibits: Messages Needing Attention")
        L.append("")
        L.append(
            "The thinnest resolution messages published in this window — "
            "useful as before/after material when rolling out templates:"
        )
        L.append("")
        for title, text in messaging["shortest_messages"]:
            L.append(f'- **{title}** — "{text[:200]}"')
        L.append("")

    # ── Scheduled maintenance (factual, all modes; when the feed exists) ──
    if maintenance:
        L.extend(render_maintenance(maintenance))

    # ── 3. Recommendations / risk assessment ──
    L.append(f"## 3. {MODE_SECTION3.get(mode, MODE_SECTION3['neutral'])}")
    L.append("")
    if llm.get("recommendations"):
        L.append(llm["recommendations"])
    elif recs:
        for i, rec in enumerate(recs, 1):
            L.append(f"{i}. {rec}")
        if mode == "vendor":
            L.append("")
            L.append(
                "*Run with an LLM provider configured for a fuller risk narrative "
                "and suggested questions to ask this vendor.*"
            )
    else:
        L.append(
            "No threshold-based findings triggered — incident volume, messaging "
            "consistency, and structure look solid across the analyzed incidents."
        )
    L.append("")

    # ── Appendix A: templates (LLM only) ──
    if llm.get("templates"):
        L.append("## Appendix A: Proposed Status Update Templates")
        L.append("")
        L.append(llm["templates"])
        L.append("")

    # ── Appendix B: methodology ──
    L.append("## Appendix B: Data Sources & Methodology")
    L.append("")
    L.append(f"- **Source:** {meta['source_label']}")
    if meta.get("snapshots_used"):
        L.append(
            f"- **Historical data:** {meta['snapshots_used']} Wayback Machine "
            "snapshot(s) of the page's feed, merged and deduplicated with the live data"
        )
    L.append(
        f"- **Incidents analyzed:** {stats_all['count']} "
        f"({stats_all['date_start']} → {stats_all['date_end']})"
    )
    if maintenance:
        L.append(
            f"- **Maintenance windows analyzed:** {maintenance['count']} completed "
            f"({maintenance['date_start']} → {maintenance['date_end']}), from the "
            "page's scheduled-maintenances API"
        )
    L.append(
        f"- **Quantitative analysis:** deterministic Python (parsing, classification, "
        "counting, regex checks) — reproducible from the same source data"
    )
    L.append(
        f"- **Qualitative sections:** {meta.get('llm_label') or 'no LLM used (deterministic fallbacks shown)'}"
    )
    L.append(
        "- **Limitations:** analysis reflects only what the company publishes; "
        "component classification is keyword-based; "
        + (
            "the source carries full incident timelines."
            if messaging["has_timeline"]
            else "the source carries only final updates per incident."
        )
    )
    if meta.get("focus_terms"):
        L.append(
            f"- **Focus filter:** incidents matching *{meta['focus_terms']}* "
            "(title, category, or component tags)"
        )
    L.append("")

    return "\n".join(L)
