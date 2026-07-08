"""Messaging quality, templating, and posting-cadence analysis.

Everything here is deterministic (regex + counting). Qualitative judgment
(tone, sentiment, recommendations) is delegated to the optional LLM layer.
"""

import re
from collections import Counter, defaultdict

from .parser import extract_resolved_message, extract_updates

DEFAULT_SUPPORT_PATTERNS = [
    r"https?://[^\s<\"]*(?:support|help|contact|get-help)",
]

# Common misspellings seen in status page communications
TYPO_PATTERNS = [
    (r"\becomm+erce\b", "ecommerce"),
    (r"\baccross\b", "across"),
    (r"\bintermitent\b", "intermittent"),
    (r"\bstablilized\b", "stabilized"),
    (r"\boccured\b", "occurred"),
    (r"\brecieved\b", "received"),
    (r"\bsucessful\b", "successful"),
    (r"\bneccessary\b", "necessary"),
    (r"\boccassion\b", "occasion"),
    (r"\bseperate\b", "separate"),
]

STRUCTURE_CHECKS = [
    ("Affected components listed", r"Affected components|<li>"),
    ("Root cause mentioned", r"(?i)root cause|caused by"),
    (
        "Incident timeline included",
        r"(?i)between .* and .* UTC|from .* to .* (?:UTC|PDT|EST|PST|ET|PT)",
    ),
    (
        "Structured sections",
        r"(?i)\*\*what happened|what happened:|## |### |impact:|resolution:",
    ),
    (
        "Customer action steps",
        r"(?i)replay|reconnect|refresh|re-?run|re-?enable|clear.*cache|re-?check",
    ),
    ("Apology included", r"(?i)apologize|sorry"),
    ("No-data-loss assurance", r"(?i)no data loss|no data was lost"),
]

OPENER_BUCKETS = [
    (r"(?i)^(we have|we've)", "We have / We've..."),
    (r"(?i)^(this issue|this incident)", "This issue/incident..."),
    (r"(?i)^(the issue|the incident|the team)", "The issue/team..."),
    (r"(?i)^(our (engineering|team))", "Our engineering team..."),
    (r"(?i)^(we're happy|we are happy|we are pleased)", "We're happy to report..."),
    (r"(?i)^(resolved|the .* has been resolved)", "Resolved - ..."),
    (r"(?i)^(earlier|between|on )", "Timeline lead..."),
    (r"(?i)^(thank you|thanks)", "Thank you..."),
    (r"(?i)^(all |everything)", "All systems..."),
]


def _support_regex(support_url_patterns):
    """Compile support-URL patterns into one case-insensitive regex.

    Patterns are wrapped in non-capturing groups so alternation composes
    safely; a leading (?i) is stripped because Python 3.11+ rejects global
    flags that aren't at the very start of a pattern.
    """
    patterns = [
        p.removeprefix("(?i)")
        for p in (support_url_patterns or DEFAULT_SUPPORT_PATTERNS)
    ]
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


def analyze_messaging(items, support_url_patterns=None):
    """Analyze messaging quality for a set of incidents. Returns a data dict."""
    support_regex = _support_regex(support_url_patterns)
    total = len(items)
    if total == 0:
        return None

    # ── Feed structure: full timeline (Atlassian) vs final-update-only (incident.io) ──
    update_counts = [len(extract_updates(i["desc_raw"])) for i in items]
    has_timeline = any(update_counts)

    statuses = Counter()
    for i in items:
        desc = i["desc_raw"]
        for status in ["Resolved", "Complete", "Monitoring", "Investigating"]:
            if f"Status: {status}" in desc or f"<strong>{status}</strong>" in desc:
                statuses[status] += 1
                break
        else:
            statuses["Other"] += 1

    # ── Message length ──
    lengths = sorted(len(i["desc_text"]) for i in items)
    length_buckets = {
        "< 100 chars (minimal)": sum(1 for n in lengths if n < 100),
        "100–300 chars (brief)": sum(1 for n in lengths if 100 <= n < 300),
        "300–600 chars (detailed)": sum(1 for n in lengths if 300 <= n < 600),
        "> 600 chars (thorough)": sum(1 for n in lengths if n >= 600),
    }
    minimal_incidents = [
        (i["title"], i["desc_text"][:120]) for i in items if len(i["desc_text"]) < 100
    ]

    # ── Structural consistency ──
    structure = []
    for name, pattern in STRUCTURE_CHECKS:
        count = sum(1 for i in items if re.search(pattern, i["desc_raw"]))
        structure.append({"check": name, "count": count, "pct": count / total * 100})
    support_count = sum(1 for i in items if support_regex.search(i["desc_raw"]))
    structure.insert(
        1,
        {
            "check": "Support link included",
            "count": support_count,
            "pct": support_count / total * 100,
        },
    )

    # ── Opening-line patterns (template detection) ──
    openers = Counter()
    for i in items:
        opener = extract_resolved_message(i["desc_raw"])[:60].strip()
        for pattern, bucket in OPENER_BUCKETS:
            if re.match(pattern, opener):
                openers[bucket] += 1
                break
        else:
            openers[f'Other: "{opener[:40]}..."'] += 1

    # ── Support link variants ──
    link_variants = Counter()
    for i in items:
        for link in re.findall(r"https?://[^\s<\"]+", i["desc_raw"]):
            link = link.rstrip(".),;")
            if support_regex.search(link):
                link_variants[link] += 1
    no_support_link = [
        i["title"] for i in items if not support_regex.search(i["desc_raw"])
    ]

    # ── Quality issues ──
    quality_issues = []
    for i in items:
        title, text = i["title"], i["desc_text"]
        combined = title + " " + text
        for pattern, correct in TYPO_PATTERNS:
            m = re.search(pattern, combined, re.IGNORECASE)
            if m:
                quality_issues.append((title, f"Typo: '{m.group(0)}' → '{correct}'"))
        if re.search(r"[a-z]\.[A-Z]", text):
            quality_issues.append((title, "Missing space between sentences"))
        m = re.search(r"\b(\w+)\s+\1\b", text)
        if m:
            quality_issues.append((title, f"Repeated word: '{m.group(0)}'"))
        resolved_msg = extract_resolved_message(i["desc_raw"])
        if resolved_msg and len(resolved_msg) < 30:
            quality_issues.append(
                (title, f'Extremely terse resolution: "{resolved_msg}"')
            )
        if re.match(r"^Service Disruption$", title.strip()):
            quality_issues.append(
                (title, "Vague title — no component or impact specified")
            )

    # Shortest resolution messages — exhibit material for internal audits
    resolutions = [
        (i["title"], extract_resolved_message(i["desc_raw"])) for i in items
    ]
    shortest_messages = sorted(resolutions, key=lambda t: len(t[1]))[:10]

    return {
        "count": total,
        "has_timeline": has_timeline,
        "shortest_messages": shortest_messages,
        "updates_per_incident": (
            {
                "min": min(update_counts),
                "max": max(update_counts),
                "avg": sum(update_counts) / total,
            }
            if has_timeline
            else None
        ),
        "statuses": statuses,
        "lengths": {
            "min": lengths[0],
            "max": lengths[-1],
            "median": lengths[len(lengths) // 2],
            "avg": sum(lengths) // total,
        },
        "length_buckets": length_buckets,
        "minimal_incidents": minimal_incidents,
        "structure": structure,
        "openers": openers,
        "link_variants": link_variants,
        "no_support_link": no_support_link,
        "quality_issues": quality_issues,
    }


def analyze_cadence(items):
    """Analyze posting time patterns for automation signals. Returns a data dict."""
    hours = Counter()
    minutes = Counter()
    dated = [i for i in items if i["pub_date"]]
    for i in dated:
        hours[i["pub_date"].hour] += 1
        minutes[i["pub_date"].minute] += 1

    round_count = sum(minutes.get(m, 0) for m in (0, 15, 30, 45))
    round_pct = round_count / len(dated) * 100 if dated else 0.0

    by_date = defaultdict(list)
    for i in dated:
        by_date[i["pub_date"].strftime("%Y-%m-%d")].append(i)

    clusters = []
    for date in sorted(by_date):
        incs = sorted(by_date[date], key=lambda x: x["pub_date"])
        if len(incs) < 2:
            continue
        gaps = [
            (incs[j]["pub_date"] - incs[j - 1]["pub_date"]).total_seconds() / 60
            for j in range(1, len(incs))
        ]
        clusters.append(
            {
                "date": date,
                "incidents": [
                    (i["pub_date"].strftime("%H:%M"), i["title"]) for i in incs
                ],
                "min_gap_minutes": min(gaps),
            }
        )

    return {
        "dated_count": len(dated),
        "hours": hours,
        "round_minute_count": round_count,
        "round_minute_pct": round_pct,
        "clusters": clusters,
    }
