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
    "openai": "gpt-4o-mini",
    "openrouter": "anthropic/claude-sonnet-4.5",
    "ollama": "llama3.1",
}

SYSTEM_PROMPT = (
    "You are an expert in site reliability engineering and incident "
    "communications. You are analyzing a company's public status page "
    "history from the outside. Be specific, evidence-based, and neutral in "
    "tone — frame findings as opportunities, not criticism. Ground every "
    "claim in the data provided. Respond in GitHub-flavored markdown with "
    "no top-level heading (your output is inserted under an existing "
    "section heading)."
)


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


def build_context(company, stats_all, messaging, cadence, comparison, sample_messages):
    """Assemble the compact JSON context shared by all LLM prompts."""
    ctx = {
        "company": company,
        "period": f"{stats_all['date_start']} to {stats_all['date_end']}",
        "total_incidents": stats_all["count"],
        "incidents_per_week": round(stats_all["per_week"], 2),
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


def generate_sections(provider, model, context_json, rule_recommendations, progress=lambda m: None):
    """Generate all LLM report sections. Returns {section_name: markdown or None}."""
    sections = {}
    rules_text = "\n".join(f"- {r}" for r in rule_recommendations) or "(none triggered)"
    for name, template in SECTION_PROMPTS.items():
        progress(f"  Generating {name.replace('_', ' ')} via {provider}/{model}...")
        prompt = template.replace("{context}", context_json).replace(
            "{rules}", rules_text
        )
        try:
            sections[name] = complete(provider, model, prompt)
        except LLMError as e:
            progress(f"  ⚠ {name} failed: {e}")
            sections[name] = None
    return sections
