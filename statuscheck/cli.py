"""Command-line interface: statuscheck <status page URL or domain>."""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .analysis import compute_stats, export_json, split_periods
from .config import load_config
from .discover import DiscoveryError, discover_feed
from .llm import LLMError, build_context, generate_sections, resolve_provider
from .messaging import analyze_cadence, analyze_messaging
from .net import FetchError
from .parser import extract_resolved_message, merge_incidents, parse_feed_auto
from .report import render_report, rule_based_recommendations, build_comparison
from .wayback import fetch_history


def _slug(feed_url):
    host = urlparse(feed_url).netloc.lower()
    host = re.sub(r"^(www|status|trust|health|uptime)\.", "", host)
    return host.split(".")[0] or "company"


def _progress(msg):
    print(msg, flush=True)


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="statuscheck",
        description=(
            "Analyze a company's public status page and produce an incident "
            "communication & reliability report."
        ),
        epilog=(
            "Examples:\n"
            "  statuscheck status.zapier.com\n"
            "  statuscheck https://www.githubstatus.com/history.atom --no-archive\n"
            "  statuscheck acme.com --llm ollama --model llama3.1 -o reports/acme\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "target",
        help="Status page URL, feed URL, or domain (e.g. status.acme.com or acme.com)",
    )
    p.add_argument(
        "-o", "--output",
        help="Output directory (default: analyses/<company>)",
    )
    p.add_argument(
        "--config",
        help="Path to a TOML config with company-specific categories/patterns",
    )
    p.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip Wayback Machine historical retrieval (live feed only)",
    )
    p.add_argument(
        "--max-snapshots",
        type=int,
        default=8,
        help="Max Wayback snapshots to fetch (default: 8)",
    )
    p.add_argument(
        "--llm",
        choices=["auto", "anthropic", "openai", "openrouter", "ollama", "none"],
        default="auto",
        help=(
            "LLM provider for qualitative sections (default: auto — picks from "
            "available API keys, falls back to a local Ollama, else skips)"
        ),
    )
    p.add_argument("--model", help="Override the provider's default model")
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return p


def run(args):
    config = load_config(args.config)

    # ── 1. Discover and fetch the live feed ──
    target = config["feed_url"] or args.target
    try:
        feed_url, xml_current = discover_feed(target, progress=_progress)
    except (DiscoveryError, FetchError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    incidents_current = parse_feed_auto(xml_current)
    print(f"Live feed: {feed_url} ({len(incidents_current)} incidents)")
    if not incidents_current:
        print("Error: feed parsed but contains no incidents.", file=sys.stderr)
        return 1

    company = config["company"] or _slug(feed_url).capitalize()
    out_dir = Path(args.output or Path("analyses") / _slug(feed_url))
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "current.xml").write_text(xml_current)

    # ── 2. Wayback Machine history ──
    snapshots = []
    if not args.no_archive:
        snapshots = fetch_history(
            feed_url, max_snapshots=args.max_snapshots, progress=_progress
        )
        for ts, xml in snapshots:
            (raw_dir / f"wayback_{ts}.xml").write_text(xml)

    # Merge: live feed first, then snapshots newest→oldest, so the most
    # recent version of a duplicated incident wins.
    snapshot_incidents = [parse_feed_auto(xml) for _, xml in reversed(snapshots)]
    incidents = merge_incidents(incidents_current, *snapshot_incidents)
    added = len(incidents) - len(incidents_current)
    if snapshots:
        print(
            f"Merged {len(snapshots)} archived snapshot(s): "
            f"+{added} incidents beyond the live feed ({len(incidents)} total)"
        )

    # ── 3. Quantitative analysis (pure code) ──
    categories = config["categories"]
    support_patterns = config["support_url_patterns"]

    stats_all = compute_stats(incidents, "All data", categories)
    if stats_all is None:
        print("Error: no incidents had parseable dates.", file=sys.stderr)
        return 1

    split = split_periods(incidents)
    if split:
        (older, older_label), (newer, newer_label) = split
        periods = [
            compute_stats(older, older_label, categories),
            compute_stats(newer, newer_label, categories),
        ]
        print(f"Comparing periods: {older_label} vs {newer_label}")
    else:
        periods = [stats_all]

    messaging = analyze_messaging(incidents, support_patterns)
    period_messaging = [
        analyze_messaging(p["incidents"], support_patterns) for p in periods
    ]
    cadence = analyze_cadence(incidents)

    # ── 4. Qualitative sections (optional LLM) ──
    llm_sections = {}
    llm_label = None
    try:
        resolved = resolve_provider(args.llm, args.model)
    except LLMError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if resolved:
        provider, model = resolved
        llm_label = f"{provider}/{model}"
        print(f"Generating qualitative sections with {llm_label}...")
        comparison = build_comparison(periods[0], periods[1]) if len(periods) == 2 else None
        recs = rule_based_recommendations(messaging, cadence, comparison)
        samples = _sample_messages(incidents)
        context = build_context(company, stats_all, messaging, cadence, comparison, samples)
        llm_sections = generate_sections(provider, model, context, recs, progress=_progress)
    elif args.llm == "auto":
        print(
            "No LLM configured (set ANTHROPIC_API_KEY / OPENAI_API_KEY / "
            "OPENROUTER_API_KEY, or run Ollama) — qualitative sections will "
            "use deterministic fallbacks."
        )

    # ── 5. Render and write outputs ──
    report_md = render_report(
        company=company,
        stats_all=stats_all,
        periods=periods,
        messaging=messaging,
        period_messaging=period_messaging,
        cadence=cadence,
        llm_sections=llm_sections,
        meta={
            "feed_url": feed_url,
            "snapshots_used": len(snapshots),
            "llm_label": llm_label,
        },
    )
    report_path = out_dir / "report.md"
    report_path.write_text(report_md)
    export_json(periods if len(periods) == 2 else [stats_all], out_dir / "incidents.json")

    print()
    print(f"✔ {stats_all['count']} incidents analyzed "
          f"({stats_all['date_start']} → {stats_all['date_end']})")
    print(f"✔ Report:    {report_path}")
    print(f"✔ Data:      {out_dir / 'incidents.json'}")
    print(f"✔ Raw feeds: {raw_dir}/")
    return 0


def _sample_messages(incidents, max_samples=25, max_chars=400):
    """Pick an evenly-spread sample of resolution messages for LLM context."""
    dated = sorted(
        (i for i in incidents if i["pub_date"]), key=lambda i: i["pub_date"]
    )
    if not dated:
        dated = incidents
    step = max(1, len(dated) // max_samples)
    samples = []
    for i in dated[::step][:max_samples]:
        samples.append(
            {
                "date": i["pub_date"].strftime("%Y-%m-%d") if i["pub_date"] else None,
                "title": i["title"],
                "resolution": extract_resolved_message(i["desc_raw"])[:max_chars],
            }
        )
    return samples


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
