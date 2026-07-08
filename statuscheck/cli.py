"""Command-line interface.

    statuscheck <status page URL or domain>     analyze and write a report
    statuscheck init <status page URL or domain>  generate a starter config
"""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .analysis import compute_stats, export_json, split_periods
from .config import load_config
from .discover import DiscoveryError, discover_source
from .lifecycle import analyze_lifecycle
from .llm import LLMError, build_context, generate_sections, resolve_provider
from .messaging import analyze_cadence, analyze_messaging
from .net import FetchError
from .parser import extract_resolved_message, merge_incidents, parse_feed_auto
from .report import build_comparison, render_report, rule_based_recommendations
from .wayback import fetch_history

LLM_CHOICES = ["auto", "anthropic", "openai", "openrouter", "ollama", "none"]


def _slug(url):
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
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
            "  statuscheck www.githubstatus.com --html --open\n"
            "  statuscheck acme.com --llm ollama --model llama3.1 -o reports/acme\n"
            "  statuscheck init acme.com          # generate a starter config\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "target",
        nargs="?",
        help=(
            "Status page URL, feed URL, or domain (e.g. status.acme.com or "
            "acme.com). Optional if --config sets feed_url."
        ),
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
        "--source",
        choices=["auto", "json", "feed"],
        default="auto",
        help=(
            "Data source: 'json' forces the Statuspage JSON API, 'feed' forces "
            "RSS/Atom, 'auto' prefers JSON when available (default)"
        ),
    )
    p.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip Wayback Machine historical retrieval (live data only)",
    )
    p.add_argument(
        "--max-snapshots",
        type=int,
        default=8,
        help="Max Wayback snapshots to fetch (default: 8)",
    )
    p.add_argument(
        "--llm",
        choices=LLM_CHOICES,
        default="auto",
        help=(
            "LLM provider for qualitative sections (default: auto — picks from "
            "available API keys, falls back to a local Ollama, else skips)"
        ),
    )
    p.add_argument("--model", help="Override the provider's default model")
    p.add_argument(
        "--html",
        action="store_true",
        help="Also render the report as a self-contained report.html",
    )
    p.add_argument(
        "--open",
        action="store_true",
        help="Open the HTML report in a browser when done (implies --html)",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return p


def build_init_parser():
    p = argparse.ArgumentParser(
        prog="statuscheck init",
        description=(
            "Generate a starter TOML config for a company from its own status "
            "page data (component list, incident titles, support links)."
        ),
    )
    p.add_argument("target", help="Status page URL, feed URL, or domain")
    p.add_argument(
        "-o", "--output",
        help="Path for the generated TOML (default: <company>.toml)",
    )
    p.add_argument(
        "--llm",
        choices=LLM_CHOICES,
        default="auto",
        help="LLM provider used to group components into categories (default: auto)",
    )
    p.add_argument("--model", help="Override the provider's default model")
    return p


def run(args):
    config = load_config(args.config)

    # ── 1. Discover and fetch the live source ──
    # An explicit CLI target beats the config's feed_url
    target = args.target or config["feed_url"]
    if not target:
        print(
            "Error: no target given. Pass a status page URL/domain, or set "
            "feed_url in the config file.",
            file=sys.stderr,
        )
        return 1

    try:
        source = discover_source(target, progress=_progress, prefer=args.source)
    except (DiscoveryError, FetchError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if source["type"] == "json":
        incidents_current = source["incidents"]
        primary_url = source["page_url"]
        source_label = f"Statuspage-compatible JSON API ({source['api_url']})"
        # The companion feed often carries incidents the API window misses;
        # merge it in (API versions win for duplicates — they're richer)
        if source.get("feed_xml"):
            feed_incidents = parse_feed_auto(source["feed_xml"])
            incidents_current = merge_incidents(incidents_current, feed_incidents)
            source_label += f" + feed ({source['feed_url']})"
    else:
        incidents_current = parse_feed_auto(source["xml"])
        primary_url = source["feed_url"]
        source_label = f"RSS/Atom feed ({source['feed_url']})"
    print(f"Source: {source_label.split(' (')[0]}, {len(incidents_current)} incidents")
    if not incidents_current:
        print("Error: source responded but contains no incidents.", file=sys.stderr)
        return 1

    company = config["company"] or _slug(primary_url).capitalize()
    out_dir = Path(args.output or Path("analyses") / _slug(primary_url))
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if source["type"] == "json":
        (raw_dir / "current.json").write_text(source["raw"], encoding="utf-8")
    else:
        (raw_dir / "current.xml").write_text(source["xml"], encoding="utf-8")

    # ── 2. Wayback Machine history (via the page's feed) ──
    snapshots = []
    feed_url = source.get("feed_url")
    if not args.no_archive:
        if feed_url:
            snapshots = fetch_history(
                feed_url, max_snapshots=args.max_snapshots, progress=_progress
            )
            for ts, xml in snapshots:
                (raw_dir / f"wayback_{ts}.xml").write_text(xml, encoding="utf-8")
        else:
            print("No companion feed found for Wayback history; skipping archive retrieval.")

    # Merge: live data first, then snapshots newest→oldest, so the most
    # recent version of a duplicated incident wins.
    snapshot_incidents = [parse_feed_auto(xml) for _, xml in reversed(snapshots)]
    incidents = merge_incidents(incidents_current, *snapshot_incidents)
    if snapshots:
        added = len(incidents) - len(incidents_current)
        print(
            f"Merged {len(snapshots)} archived snapshot(s): "
            f"+{added} incidents beyond the live data ({len(incidents)} total)"
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
    lifecycle = analyze_lifecycle(incidents)
    if lifecycle:
        print(
            f"Lifecycle timing available for {lifecycle['covered']}/{lifecycle['total']} incidents"
        )

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
        recs = rule_based_recommendations(messaging, cadence, comparison, lifecycle)
        samples = _sample_messages(incidents)
        context = build_context(
            company, stats_all, messaging, cadence, comparison, samples, lifecycle
        )
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
            "source_label": source_label,
            "snapshots_used": len(snapshots),
            "llm_label": llm_label,
        },
        lifecycle=lifecycle,
    )
    report_path = out_dir / "report.md"
    report_path.write_text(report_md, encoding="utf-8")
    export_json(periods if len(periods) == 2 else [stats_all], out_dir / "incidents.json")

    print()
    print(f"✔ {stats_all['count']} incidents analyzed "
          f"({stats_all['date_start']} → {stats_all['date_end']})")
    print(f"✔ Report:    {report_path}")

    if args.html or args.open:
        from .htmlout import render_html

        html_path = out_dir / "report.html"
        html_path.write_text(
            render_html(report_md, title=f"{company} Incident Assessment"),
            encoding="utf-8",
        )
        print(f"✔ HTML:      {html_path}")
        if args.open:
            import webbrowser

            webbrowser.open(html_path.resolve().as_uri())

    print(f"✔ Data:      {out_dir / 'incidents.json'}")
    print(f"✔ Raw data:  {raw_dir}/")
    return 0


def run_init(args):
    from .initcfg import (
        collect_components,
        derive_support_patterns,
        heuristic_categories,
        incidents_from_source,
        llm_categories,
        render_toml,
    )

    try:
        source = discover_source(args.target, progress=_progress)
    except (DiscoveryError, FetchError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    incidents = incidents_from_source(source)
    if not incidents:
        print("Error: source responded but contains no incidents.", file=sys.stderr)
        return 1

    target_url = source["page_url"] if source["type"] == "json" else source["feed_url"]
    slug = _slug(target_url)
    company = slug.capitalize()

    components = collect_components(source, incidents)
    print(f"Found {len(components)} components, {len(incidents)} incidents")
    support_patterns = derive_support_patterns(incidents)

    categories = None
    taxonomy_source = "heuristic (one category per component)"
    try:
        resolved = resolve_provider(args.llm, args.model)
    except LLMError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if resolved:
        provider, model = resolved
        print(f"Grouping components into categories with {provider}/{model}...")
        try:
            titles = [i["title"] for i in incidents]
            categories = llm_categories(provider, model, components, titles)
            taxonomy_source = f"{provider}/{model}"
        except (LLMError, ValueError) as e:
            print(f"  ⚠ LLM taxonomy failed ({e}); using heuristic instead.")
    if categories is None:
        categories = heuristic_categories(components)

    out_path = Path(args.output or f"{slug}.toml")
    out_path.write_text(
        render_toml(company, target_url, support_patterns, categories, taxonomy_source),
        encoding="utf-8",
    )
    print()
    print(f"✔ Wrote {out_path} ({len(categories)} categories, "
          f"{len(support_patterns)} support-link patterns)")
    print(f"  Review it, then run:  statuscheck --config {out_path}")
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
    # Reports and progress output use ✔/█/→; don't crash on consoles with
    # legacy encodings (e.g. cp1252 on Windows)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    argv = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        if argv[:1] == ["init"]:
            return run_init(build_init_parser().parse_args(argv[1:]))
        return run(build_arg_parser().parse_args(argv))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
