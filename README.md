# status-page-check

Analyze any company's public status page and get a reliability & incident-communication report — in one command.

```bash
statuscheck status.zapier.com
```

The tool discovers the status page's data sources (Statuspage-compatible JSON API and/or RSS/Atom feed), pulls historical copies from the Internet Archive's Wayback Machine to extend the analysis window, and produces a report covering:

- **Incident frequency & trends** — volume, rate, monthly distribution, period-over-period comparison
- **Severity mix** — the page's own declared impact levels (critical/major/minor), when available
- **Incident lifecycle** — median/p90 time to resolve, % resolved within 1/4/24 h, longest incidents, and the longest silences between updates during open incidents
- **Disclosed downtime** — total major/critical degradation hours and implied disclosed availability
- **Transparency signals** — postmortem rates, severity honesty, late-disclosure (backfill) detection
- **Component analysis** — what breaks most, how that's shifting over time
- **Failure modes** — delays vs errors vs outages
- **Operational patterns** — day-of-week, hour-of-day coverage, same-day incident clustering
- **Messaging quality** — template consistency, detail level, structure completeness, support-link hygiene, typos
- **Automation signals** — manual vs scheduled posting, batch-posting detection
- **Tone & sentiment assessment, recommendations (or vendor risk analysis), and proposed templates** — via an optional LLM (Anthropic, OpenAI, OpenRouter, or local Ollama)

All quantitative analysis is deterministic Python — reproducible from the same feed data, no AI required. The LLM is used only for the judgment calls code can't make: assessing tone, summarizing, and refining recommendations. Without an LLM configured, the report still generates with deterministic fallbacks (including rule-based recommendations).

## Two ways to use it

**Audit your own status page** (`--mode self`) — an internal working document for your incident/comms team: candid recommendations ordered by impact-vs-effort, the full (untruncated) list of quality issues and thin messages, an exhibits section with your weakest resolution messages for template rollout discussions, and proposed status-update templates.

```bash
statuscheck status.mycompany.com --mode self --llm anthropic --html
```

**Evaluate a potential vendor** (`--mode vendor`) — a due-diligence document for a team deciding whether to take a dependency: the executive summary answers "how risky is this?", section 3 becomes a risk assessment with suggested questions to ask the vendor in procurement review, and templates are dropped. Combine with `--focus` to restrict analysis to the components you'd actually depend on:

```bash
statuscheck vendor.com --mode vendor --focus "API, Webhooks" --html --open
```

Two vendor-oriented measurements ship in every report when the data supports them:

- **Disclosed downtime** — total duration of major/critical incidents over the observed window, and the implied disclosed availability (e.g. "8 major/critical incidents totaled 14h 10m over 70 days ≈ 99.16%"). This is a *floor* on downtime: it counts only what the company published, and severity is self-declared.
- **Transparency signals** — severity coverage, postmortem/root-cause rates on major incidents, whether the page has *ever* declared an incident above "minor", and incidents disclosed 24+ hours after their declared start (backfilling).

## Install

Requires Python 3.11+. No dependencies — standard library only.

```bash
pip install git+https://github.com/jeffcampbell/status-page-check.git
# or, for development:
git clone https://github.com/jeffcampbell/status-page-check.git
cd status-page-check
pip install -e .
```

## Usage

```bash
# Point it at anything: a domain, a status page, or a feed URL
statuscheck acme.com
statuscheck status.zapier.com
statuscheck https://www.githubstatus.com/history.atom

# Frame the report for your use case (see "Two ways to use it")
statuscheck status.mycompany.com --mode self
statuscheck vendor.com --mode vendor --focus "API, Webhooks"

# Also render a shareable single-file HTML report, and open it
statuscheck status.zapier.com --html --open

# Skip the Wayback Machine (live data only, faster)
statuscheck status.zapier.com --no-archive

# Pick an LLM provider explicitly
statuscheck status.zapier.com --llm anthropic
statuscheck status.zapier.com --llm ollama --model llama3.1
statuscheck status.zapier.com --llm none          # fully deterministic

# Bootstrap a company-specific config from the page's own component list,
# then run with it
statuscheck init acme.com
statuscheck --config acme.toml
```

Outputs land in `analyses/<company>/`:

```
analyses/zapier/
├── report.md         # the assessment report
├── report.html       # with --html/--open: self-contained shareable HTML
├── incidents.json    # parsed incident data
└── raw/              # raw source data (live + Wayback snapshots), for reproducibility
```

## How it works

1. **Source discovery** — first probes for a Statuspage-compatible JSON API (`/api/v2/incidents.json`), which carries declared severity, component tags, and the full timestamped update history. Both Atlassian Statuspage and incident.io serve one. Falls back to RSS/Atom feed discovery (advertised `<link rel="alternate">` tags and common provider paths), trying `status.acme.com`, `trust.acme.com`, etc. when given a bare domain. When both exist they're merged — the API is richer but the feed often reaches further back.
2. **History retrieval** — live sources are rolling windows (Statuspage's API returns the 50 most recent incidents), so the tool queries the Wayback Machine's CDX API for archived snapshots of the page's feed, fetches a monthly spread of them (raw `id_` variants), and merges everything with dedup by incident link.
3. **Analysis** — parsing, classification, counting, duration math, and regex checks in plain Python. If the merged data spans a year or more, it's split at the midpoint for period-over-period comparison.
4. **Report** — rendered entirely in code (markdown, plus optional single-file HTML), including threshold-based recommendations. If an LLM is available, it additionally writes the executive summary, a tone/sentiment assessment grounded in sampled messages, refined recommendations, and proposed status-update templates.

## LLM providers

By default (`--llm auto`) the tool uses the first available provider, or skips LLM sections entirely if none is configured:

| Provider | Setup | Default model |
|----------|-------|---------------|
| `anthropic` | `export ANTHROPIC_API_KEY=...` | `claude-sonnet-4-6` |
| `openai` | `export OPENAI_API_KEY=...` | `gpt-4.1-mini` |
| `openrouter` | `export OPENROUTER_API_KEY=...` | `anthropic/claude-sonnet-4.5` |
| `ollama` | run Ollama locally (`OLLAMA_HOST` to override the default `http://localhost:11434`) | `llama3.1` |

Override the model with `--model`. Providers are called over plain HTTP — no SDKs.

What gets sent to the provider: aggregate statistics plus a sample of up to 25 resolution messages from the (already public) status page. Nothing else.

## Per-company configuration

The built-in component taxonomy is generic. `statuscheck init acme.com` generates a starter config from the company's own component list and incident history — with an LLM configured it groups components into sensible categories, otherwise it maps one category per component. Review the generated TOML, then pass it with `--config`. (You can also start from [`examples/config.example.toml`](examples/config.example.toml) by hand.) A practical loop: run once, look at what lands in "Other" in section 1.3, refine, re-run.

## Caveats

- incident.io-style **feeds** contain only the **final update** per incident, so for those sources messaging analysis reflects resolution communications and lifecycle metrics cover only incidents the JSON API window includes. Statuspage feeds and both JSON APIs carry full timelines.
- The analysis only sees what the company **chooses to publish** — it measures communication quality directly, and reliability only as reported.
- Wayback Machine coverage varies; some companies' feeds were never archived.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT
