"""Optional LLM layer for qualitative analysis.

All quantitative work is done in plain Python; the LLM is used only for
judgment calls that code can't make well:
  - executive summary narrative
  - tone / sentiment assessment of the actual messages
  - refining rule-based recommendations
  - proposing status-update templates

Providers (no SDKs — raw HTTP via urllib):
  anthropic   Anthropic Messages API        needs ANTHROPIC_API_KEY
  openai      OpenAI chat completions       needs OPENAI_API_KEY
  openrouter  OpenRouter (OpenAI-compatible) needs OPENROUTER_API_KEY
  ollama      Local Ollama (OpenAI-compatible) needs a running Ollama server
"""

import json
import os
import urllib.request

from .net import FetchError, http_post_json

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4.1-mini",
    "openrouter": "anthropic/claude-sonnet-4.5",
    "ollama": "llama3.1",
}

_STYLE_RULES = (
    "Be specific and evidence-based; ground every claim in the data "
    "provided. Respond in GitHub-flavored markdown with no top-level "
    "heading (your output is inserted under an existing section heading)."
)

SYSTEM_PROMPTS = {
    "neutral": (
        "You are an expert in site reliability engineering and incident "
        "communications, analyzing a company's public status page history "
        "from the outside. Keep a neutral tone — frame findings as "
        "opportunities, not criticism. " + _STYLE_RULES
    ),
    "self": (
        "You are an expert in site reliability engineering and incident "
        "communications, advising this company's own incident/comms team on "
        "an internal audit of their public status page. Address the team "
        "directly and be candid — they asked for this review and want to "
        "improve, not be flattered. " + _STYLE_RULES
    ),
    "vendor": (
        "You are an expert in site reliability engineering, advising a team "
        "that is evaluating this company as a potential vendor they would "
        "depend on. Your reader's question is 'how risky is this dependency, "
        "and what should we verify before signing?' — not how the vendor "
        "could improve. Note that incident volume also reflects disclosure "
        "practices: a transparent vendor can look worse than a secretive "
        "one. " + _STYLE_RULES
    ),
}

SYSTEM_PROMPT = SYSTEM_PROMPTS["neutral"]  # default for direct complete() calls


class LLMError(Exception):
    """Provider resolution or completion failed."""


def _ollama_host():
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def _ollama_reachable():
    try:
        req = urllib.request.Request(_ollama_host() + "/api/tags")
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception:
        return False


def resolve_provider(choice="auto", model=None):
    """Resolve (provider, model) from a CLI choice plus environment.

    Returns None when choice is "none" or when "auto" finds no credentials.
    """
    if choice == "none":
        return None
    if choice == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            choice = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            choice = "openai"
        elif os.environ.get("OPENROUTER_API_KEY"):
            choice = "openrouter"
        elif _ollama_reachable():
            choice = "ollama"
        else:
            return None

    if choice not in DEFAULT_MODELS:
        raise LLMError(f"Unknown LLM provider: {choice}")

    env_var = f"{choice.upper()}_API_KEY"
    if choice != "ollama" and not os.environ.get(env_var):
        raise LLMError(f"Provider '{choice}' selected but {env_var} is not set.")
    if choice == "ollama" and not _ollama_reachable():
        raise LLMError(
            f"Ollama not reachable at {_ollama_host()} (set OLLAMA_HOST to override)."
        )

    return choice, (model or DEFAULT_MODELS[choice])


def complete(provider, model, prompt, system=SYSTEM_PROMPT):
    """Run one completion and return the text response."""
    try:
        if provider == "anthropic":
            resp = http_post_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "model": model,
                    "max_tokens": 4000,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                    "anthropic-version": "2023-06-01",
                },
            )
            return "".join(
                block.get("text", "") for block in resp.get("content", [])
            ).strip()

        # OpenAI-compatible chat completions (openai, openrouter, ollama)
        if provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
        elif provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                "X-Title": "status-page-check",
            }
        elif provider == "ollama":
            url = _ollama_host() + "/v1/chat/completions"
            headers = {}
        else:
            raise LLMError(f"Unknown provider: {provider}")

        resp = http_post_json(
            url,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
            headers=headers,
        )
        return resp["choices"][0]["message"]["content"].strip()

    except FetchError as e:
        raise LLMError(str(e))
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected response shape from {provider}: {e}")


def build_context(
    company, stats_all, messaging, cadence, comparison, sample_messages,
    lifecycle=None, transparency=None, downtime=None, focus_terms=None,
):
    """Assemble the compact JSON context shared by all LLM prompts."""
    ctx = {
        "company": company,
        "focus": (
            f"Analysis restricted to components matching: {focus_terms}"
            if focus_terms
            else None
        ),
        "transparency_signals": (
            {
                "severity_coverage_pct": round(transparency["severity_coverage_pct"]),
                "major_or_critical_count": transparency["major_count"],
                "never_declared_above_minor": transparency["never_above_minor"],
                "postmortem_rate_pct": round(transparency["postmortem_rate_pct"]),
                "postmortem_basis": transparency["postmortem_basis"],
                "incidents_disclosed_24h_late": len(transparency["backfilled"]),
            }
            if transparency
            else None
        ),
        "disclosed_downtime": (
            {
                "basis": downtime["basis"],
                "incident_count": downtime["incident_count"],
                "total_hours": round(downtime["total_minutes"] / 60, 1),
                "window_days": downtime["window_days"],
                "implied_disclosed_availability_pct": round(
                    downtime["availability_pct"], 2
                ),
            }
            if downtime
            else None
        ),
        "period": f"{stats_all['date_start']} to {stats_all['date_end']}",
        "total_incidents": stats_all["count"],
        "incidents_per_week": round(stats_all["per_week"], 2),
        "severity_distribution": dict(stats_all.get("severity") or {}) or None,
        "lifecycle": (
            {
                "coverage": f"{lifecycle['covered']}/{lifecycle['total']} incidents",
                "median_resolve_minutes": round(lifecycle["median_minutes"]),
                "p90_resolve_minutes": round(lifecycle["p90_minutes"]),
                "max_resolve_minutes": round(lifecycle["max_minutes"]),
                "pct_resolved_within_4h": round(lifecycle["within"]["4h"]),
                "median_longest_gap_between_updates_minutes": (
                    round(lifecycle["gap_median_minutes"])
                    if lifecycle["gap_median_minutes"] is not None
                    else None
                ),
            }
            if lifecycle
            else None
        ),
        "top_components": stats_all["components"].most_common(8),
        "issue_keywords_in_titles": dict(stats_all["keywords"]),
        "structure_checks_pct": {
            s["check"]: round(s["pct"]) for s in messaging["structure"]
        },
        "message_length_chars": messaging["lengths"],
        "resolution_opening_patterns": dict(messaging["openers"].most_common(12)),
        "support_link_variants": dict(messaging["link_variants"].most_common(8)),
        "quality_issues": messaging["quality_issues"][:20],
        "posting_round_minute_pct": round(cadence["round_minute_pct"]),
        "same_day_cluster_count": len(cadence["clusters"]),
        "period_comparison": comparison,
        "sample_resolution_messages": sample_messages,
    }
    return json.dumps(ctx, indent=1, default=str)


SECTION_PROMPTS = {
    "executive_summary": (
        "Write a single-paragraph executive summary (4-6 sentences) of this "
        "company's incident record and communication quality based on the data "
        "below. Lead with the most consequential finding.\n\nDATA:\n{context}"
    ),
    "tone_assessment": (
        "Assess the tone, sentiment, and empathy of this company's incident "
        "messages, using the sample resolution messages below as primary "
        "evidence. Cover: overall voice (formal/casual, warm/robotic), "
        "consistency between messages, how ownership and apology are handled, "
        "and whether customers get what they need emotionally and practically. "
        "Quote short fragments from the samples as evidence. 2-4 paragraphs.\n\n"
        "DATA:\n{context}"
    ),
    "recommendations": (
        "Below is data about this company's incident communications, plus "
        "rule-based recommendations generated from thresholds. Refine, "
        "reorder by impact, and extend this list. Keep every recommendation "
        "actionable and tied to a specific number in the data. Output a "
        "numbered list where each item has a bold one-line recommendation "
        "followed by 1-3 sentences of rationale.\n\n"
        "RULE-BASED RECOMMENDATIONS:\n{rules}\n\nDATA:\n{context}"
    ),
    "templates": (
        "Based on the data below (especially the opening-pattern variety and "
        "structure-check percentages), propose status-update templates this "
        "company could adopt: Investigating, Identified, Monitoring, and "
        "Resolved. Use placeholders like [component] and [impact window]. Tailor "
        "the templates to this company's actual incident types (see "
        "top_components and sample messages). After the four templates, add a "
        "short required-fields checklist for the Resolved message.\n\n"
        "DATA:\n{context}"
    ),
}

# Per-mode section overrides. A value of None drops the section entirely.
MODE_SECTION_OVERRIDES = {
    "neutral": {},
    "self": {
        "executive_summary": (
            "Write a single-paragraph executive summary (4-6 sentences) of "
            "this internal audit of your company's public incident "
            "communications, based on the data below. Lead with the most "
            "consequential finding and the clearest opportunity to improve."
            "\n\nDATA:\n{context}"
        ),
        "recommendations": (
            "You are advising this company's own incident-communications "
            "team. Below is their data plus threshold-triggered "
            "recommendations. Refine and extend the list, ordered by "
            "impact-vs-effort (quick wins first). Output a numbered list "
            "where each item has a bold one-line recommendation, 1-3 "
            "sentences of rationale tied to a specific number in the data, "
            "and a concrete first step the team could take this week.\n\n"
            "RULE-BASED RECOMMENDATIONS:\n{rules}\n\nDATA:\n{context}"
        ),
    },
    "vendor": {
        "executive_summary": (
            "Write a single-paragraph executive summary (4-6 sentences) for "
            "a team deciding whether to take a dependency on this company. "
            "Answer: how reliable does the service look (especially any "
            "focused components), is it improving or degrading, and how "
            "honest and mature are its incident communications.\n\n"
            "DATA:\n{context}"
        ),
        "recommendations": (
            "You are helping a team evaluate this company as a potential "
            "vendor. Based on the data and the observed risk signals below, "
            "produce two parts: (1) a numbered list of concrete risks of "
            "depending on this vendor, each tied to a specific number in the "
            "data, ordered by severity; (2) a short bulleted list of "
            "questions to ask the vendor in a procurement or security "
            "review. Do NOT address advice to the vendor.\n\n"
            "OBSERVED RISK SIGNALS:\n{rules}\n\nDATA:\n{context}"
        ),
        "templates": None,  # proposing templates to a vendor is pointless
    },
}


def prompts_for_mode(mode):
    """Section prompts for a report mode; None-valued sections are dropped."""
    prompts = dict(SECTION_PROMPTS)
    for name, override in MODE_SECTION_OVERRIDES.get(mode, {}).items():
        if override is None:
            prompts.pop(name, None)
        else:
            prompts[name] = override
    return prompts


def generate_sections(
    provider, model, context_json, rule_recommendations,
    progress=lambda m: None, mode="neutral",
):
    """Generate all LLM report sections. Returns {section_name: markdown or None}."""
    sections = {}
    rules_text = "\n".join(f"- {r}" for r in rule_recommendations) or "(none triggered)"
    system = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPT)
    for name, template in prompts_for_mode(mode).items():
        progress(f"  Generating {name.replace('_', ' ')} via {provider}/{model}...")
        prompt = template.replace("{context}", context_json).replace(
            "{rules}", rules_text
        )
        try:
            sections[name] = complete(provider, model, prompt, system=system)
        except LLMError as e:
            progress(f"  ⚠ {name} failed: {e}")
            sections[name] = None
    return sections
