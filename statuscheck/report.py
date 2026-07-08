"""Markdown report generation.

Renders the full assessment report from computed data. LLM-generated
sections (executive summary, tone assessment, refined recommendations,
templates) are inserted when available; every section has a deterministic
fallback so the report is complete without any LLM.
"""

from datetime import datetime, timezone

DOW_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _bar(count, max_count, width=40):
    if max_count <= 0:
        return ""
    return "█" * max(1, round(count / max_count * width)) if count else ""


def _table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


def rule_based_recommendations(messaging, cadence, comparison=None):
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

    minimal = messaging["length_buckets"].get("< 100 chars (minimal)", 0)
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

    return recs


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
    meta,             # feed_url, snapshots_used, llm_label, feed_has_timeline
):
    comparison = build_comparison(periods[0], periods[1]) if len(periods) == 2 else None
    recs = rule_based_recommendations(messaging, cadence, comparison)
    llm = llm_sections or {}
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    L = []
    L.append(f"# {company} Incident Management: External Assessment")
    L.append("")
    L.append(
        f"*Generated {generated} by [status-page-check]"
        f"(https://github.com/jeffcampbell/status-page-check) from public status page data.*"
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
    L.extend(_table(["Period", "Dates", "Incidents", "Per week"], rows))
    if comparison:
        arrow = "▲" if comparison["rate_change_pct"] > 0 else "▼"
        L.append("")
        L.append(
            f"Rate change between periods: {arrow} "
            f"**{abs(comparison['rate_change_pct']):.0f}%**"
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

    # ── 2. Messaging analysis ──
    L.append("## 2. Status Page Messaging Analysis")
    L.append("")
    if messaging["has_timeline"]:
        u = messaging["updates_per_incident"]
        L.append(
            f"The feed includes the full update timeline per incident "
            f"(min {u['min']}, max {u['max']}, avg {u['avg']:.1f} updates/incident)."
        )
    else:
        L.append(
            "> **Caveat:** this feed contains only the *final* update per incident "
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
        L.append("")
        L.append("Minimally-detailed incidents (< 100 chars):")
        L.append("")
        for title, text in messaging["minimal_incidents"][:10]:
            L.append(f'- **{title}** — "{text}"')
        extra = len(messaging["minimal_incidents"]) - 10
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

    # ── 3. Recommendations ──
    L.append("## 3. Recommendations")
    L.append("")
    if llm.get("recommendations"):
        L.append(llm["recommendations"])
    elif recs:
        for i, rec in enumerate(recs, 1):
            L.append(f"{i}. {rec}")
    else:
        L.append(
            "No threshold-based recommendations triggered — messaging consistency "
            "and structure look solid across the analyzed incidents."
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
    L.append(f"- **Feed:** {meta['feed_url']}")
    if meta.get("snapshots_used"):
        L.append(
            f"- **Historical data:** {meta['snapshots_used']} Wayback Machine "
            "snapshot(s) of the same feed, merged and deduplicated with the live feed"
        )
    L.append(
        f"- **Incidents analyzed:** {stats_all['count']} "
        f"({stats_all['date_start']} → {stats_all['date_end']})"
    )
    L.append(
        f"- **Quantitative analysis:** deterministic Python (parsing, classification, "
        "counting, regex checks) — reproducible from the same feed data"
    )
    L.append(
        f"- **Qualitative sections:** {meta.get('llm_label') or 'no LLM used (deterministic fallbacks shown)'}"
    )
    L.append(
        "- **Limitations:** analysis reflects only what the company publishes; "
        "component classification is keyword-based; "
        + (
            "the feed carries full incident timelines."
            if messaging["has_timeline"]
            else "the feed carries only final updates per incident."
        )
    )
    L.append("")

    return "\n".join(L)
