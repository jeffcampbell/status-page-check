# status-page-check — Project Guide

A zero-dependency Python CLI that analyzes a company's public status page and produces an incident-communication & reliability report. Retrieval (feed discovery + Wayback Machine history) and all quantitative analysis are pure code; an optional multi-provider LLM layer handles qualitative sections only.

## Architecture

```
statuscheck/
├── cli.py        # argparse entry point; `statuscheck <target>` and `statuscheck init`
├── discover.py   # target → source (JSON API preferred, RSS/Atom fallback)
├── jsonapi.py    # Statuspage-compatible /api/v2/incidents.json fetch + normalize
├── wayback.py    # Wayback Machine CDX lookup + snapshot fetching
├── parser.py     # RSS/Atom parsing, timed-update extraction, dedup/merge
├── analysis.py   # frequency/trend stats, severity, classification, period split
├── lifecycle.py  # incident duration / update-gap metrics (MTTR)
├── messaging.py  # messaging quality, structure checks, cadence (returns data dicts)
├── report.py     # markdown report renderer + rule-based recommendations
├── htmlout.py    # markdown → self-contained HTML (report subset only)
├── llm.py        # optional LLM sections (anthropic/openai/openrouter/ollama)
├── initcfg.py    # `statuscheck init`: bootstrap a per-company TOML config
├── config.py     # optional per-company TOML config loading
└── net.py        # urllib GET/POST helpers
```

Pipeline (see `cli.run`): discover source (JSON API + companion feed, merged) → fetch & merge Wayback snapshots of the feed → compute stats (+ midpoint period split if span ≥ 360 days) → messaging/cadence/lifecycle analysis → optional LLM sections → render `report.md` (+ `report.html` with `--html`) + `incidents.json` into `analyses/<company>/`.

## Design rules

- **Standard library only.** No third-party dependencies, including LLM SDKs — providers are called with raw `urllib` (`net.http_post_json`). Keep it that way.
- **Code vs LLM split:** anything countable, parseable, or threshold-checkable is deterministic Python. The LLM is only for judgment: executive summary, tone/sentiment, refining recommendations, drafting templates. Every LLM section must have a deterministic fallback so `--llm none` produces a complete report.
- **Analysis modules return data, not prints.** `messaging.py`/`analysis.py` return dicts; only `cli.py` prints progress and `report.py` renders markdown.
- **Keyword matching uses word boundaries** (`\b`) — plain substring matching caused "api" to match inside "Zapier".
- `analyses/` is gitignored: reports and raw feeds are per-user artifacts, not repo content.

## Testing

```bash
python -m unittest discover -s tests -v
```

Tests use inline RSS (incident.io style) and Atom (Atlassian Statuspage style) fixtures in `tests/test_statuscheck.py`. When touching the parser, add fixture coverage for the new shape. For an end-to-end smoke test: `python -m statuscheck.cli status.zapier.com --no-archive --llm none -o /tmp/smoke`.

## Status page provider quirks (institutional knowledge)

| Provider | Sources | Quirks |
|----------|---------|--------|
| Atlassian Statuspage | JSON API (`/api/v2/incidents.json`) + Atom (`/history.atom`) | API returns the **50 most recent** incidents with impact, components, timestamped updates. Atom embeds the full timeline as `<strong>Status</strong> - message` blocks, but its timestamps have **no year** and are wrapped in `<var>` tags (`Jul <var> 7</var>, 16:17 UTC`) — `extract_timed_updates` strips `<var>` and infers the year from pubDate. Times may be in the page's local tz abbreviation (mapped in `parser._TZ_OFFSETS`; unknown tz → skip). |
| incident.io | Statuspage-compatible JSON API + RSS 2.0 (`/feed.rss`) | The compat API has resolved_at + timestamped updates but a **shorter window than the RSS feed** (e.g. 25 vs 47 for Zapier) and usually empty components/impact — merge both live sources. RSS links contain a **double slash** (`host//incidents/<id>`) vs the API's single slash; `incident_key` collapses slashes so dedup works. Feed: `<content:encoded>` duplicates the description (stripped in `_clean_xml`); CDATA-wrapped; components in `<li>Component (Operational)</li>`; **only the final update per incident**. |
| Instatus | RSS 2.0 | Like incident.io's feed, may lack `content:encoded` |

Statuspage pages often carry a placeholder component like "Visit www.example.com for more information" — `initcfg._is_real_component` filters these out of generated taxonomies.

Wayback Machine: always use the `id_` URL variant (`https://web.archive.org/web/{ts}id_/{url}`) — without it the response is wrapped in archive HTML chrome. Some snapshots are archived redirects/HTML even with `id_`; `wayback.fetch_history` validates and skips them. Status page feeds are rolling windows, which is why merging archived snapshots matters for trend analysis.

Analysis caveat that must stay in reports: for final-update-only feeds, the messaging analysis covers resolution communications, not the full incident lifecycle (`report.py` emits this caveat automatically based on `messaging["has_timeline"]`).

## Extending

- **New feed provider:** extend `parse_feed_auto`/`parse_rss`/`parse_atom` in `parser.py`, add a fixture test, and add its feed path to `discover.COMMON_FEED_PATHS`. For a JSON source, follow `jsonapi.py`: normalize to the shared incident dict shape (desc_raw rebuilt in the Atom HTML shape so messaging regexes work) plus the structured extras (`impact`, `created_at`, `resolved_at`, `updates_timed`, `components`).
- **New LLM provider:** add to `llm.DEFAULT_MODELS` and `llm.complete` (OpenAI-compatible endpoints need only a URL + auth header), plus the `--llm` choices in `cli.py`.
- **New report section:** compute in `analysis.py`/`messaging.py` (return data), render in `report.py`. If it needs judgment, add a prompt to `llm.SECTION_PROMPTS` with a deterministic fallback in `report.py`.
