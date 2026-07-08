# status-page-check

Analyze any company's public status page and get a reliability & incident-communication report — in one command.

```bash
statuscheck status.zapier.com
```

The tool discovers the status page's RSS/Atom feed, pulls historical copies from the Internet Archive's Wayback Machine to extend the analysis window, and produces a markdown report covering:

- **Incident frequency & trends** — volume, rate, monthly distribution, period-over-period comparison
- **Component analysis** — what breaks most, how that's shifting over time
- **Failure modes** — delays vs errors vs outages
- **Operational patterns** — day-of-week, hour-of-day coverage, same-day incident clustering
- **Messaging quality** — template consistency, detail level, structure completeness, support-link hygiene, typos
- **Automation signals** — manual vs scheduled posting, batch-posting detection
- **Tone & sentiment assessment, recommendations, and proposed templates** — via an optional LLM (Anthropic, OpenAI, OpenRouter, or local Ollama)

All quantitative analysis is deterministic Python — reproducible from the same feed data, no AI required. The LLM is used only for the judgment calls code can't make: assessing tone, summarizing, and refining recommendations. Without an LLM configured, the report still generates with deterministic fallbacks (including rule-based recommendations).

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

# Skip the Wayback Machine (live feed only, faster)
statuscheck status.zapier.com --no-archive

# Pick an LLM provider explicitly
statuscheck status.zapier.com --llm anthropic
statuscheck status.zapier.com --llm ollama --model llama3.1
statuscheck status.zapier.com --llm none          # fully deterministic

# Company-specific tuning
statuscheck status.acme.com --config acme.toml
```

Outputs land in `analyses/<company>/`:

```
analyses/zapier/
├── report.md         # the assessment report
├── incidents.json    # parsed incident data
└── raw/              # raw feed XML (live + Wayback snapshots), for reproducibility
```

## How it works

1. **Feed discovery** — probes the target for advertised `<link rel="alternate">` feeds and common provider paths (`/feed.rss`, `/history.atom`, …), falling back from `acme.com` to `status.acme.com`, `trust.acme.com`, etc. Handles incident.io, Atlassian Statuspage, and Instatus formats.
2. **History retrieval** — status page feeds are rolling windows, so the live feed only goes back a few months. The tool queries the Wayback Machine's CDX API for archived snapshots of the same feed, fetches a monthly spread of them (raw `id_` variants), and merges everything with dedup by incident link.
3. **Analysis** — parsing, classification, counting, and regex checks in plain Python. If the merged data spans a year or more, it's split at the midpoint for period-over-period comparison.
4. **Report** — a markdown report is rendered entirely in code, including threshold-based recommendations. If an LLM is available, it additionally writes the executive summary, a tone/sentiment assessment grounded in sampled messages, refined recommendations, and proposed status-update templates.

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

The built-in component taxonomy is generic. For better classification, copy [`examples/config.example.toml`](examples/config.example.toml), tailor the categories to the company's product areas, and pass it with `--config`. A practical loop: run once with defaults, look at what lands in "Other" in section 1.3, add categories, re-run.

## Caveats

- Most status page feeds contain only the **final update** per incident (usually the Resolved message), so messaging analysis reflects resolution communications, not the full incident lifecycle. Atlassian Statuspage feeds embed full timelines, which the tool detects and uses.
- The analysis only sees what the company **chooses to publish** — it measures communication quality directly, and reliability only as reported.
- Wayback Machine coverage varies; some companies' feeds were never archived.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT
