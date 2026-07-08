"""`statuscheck init`: generate a starter per-company TOML config.

Bootstraps the component taxonomy from the status page's own data — its
component list (Statuspage JSON API) or the components tagged on past
incidents — instead of making the user hand-edit from a generic example.
If an LLM provider is available it groups the components into categories;
otherwise a one-category-per-component heuristic is used.
"""

import json
import re
from collections import Counter

from .analysis import DEFAULT_COMPONENT_CATEGORIES
from .jsonapi import fetch_component_names
from .llm import complete
from .parser import extract_affected_components, parse_feed_auto

_INIT_SYSTEM_PROMPT = (
    "You design keyword taxonomies for classifying status page incidents. "
    "Reply with a single JSON object and nothing else — no prose, no code fences."
)

_INIT_PROMPT = """Design a component taxonomy for classifying this company's \
status page incidents.

Their status page components (with incident counts where known):
{components}

Sample incident titles:
{titles}

Return a JSON object mapping 5-10 category names to lists of lowercase \
keywords. Keywords are matched on word boundaries against incident title + \
description + component tags, and the FIRST matching category wins, so order \
categories from most specific to most generic. Cover the major product areas \
these components and titles suggest. Avoid keywords so generic they'd match \
almost any incident (e.g. "issue", "error", "service")."""


def incidents_from_source(source):
    """Get the incident list out of a discover_source() result."""
    if source["type"] == "json":
        return source["incidents"]
    return parse_feed_auto(source["xml"])


# Placeholder "components" some pages carry (e.g. "Visit www.example.com
# for more information") — not real product areas
_JUNK_COMPONENT_RE = re.compile(r"(?i)visit |www\.|https?://|more information")


def _is_real_component(name):
    return bool(name) and len(name) <= 40 and not _JUNK_COMPONENT_RE.search(name)


def collect_components(source, incidents):
    """Count component mentions; for JSON sources also include the page's
    full component list (zero-count components still inform the taxonomy)."""
    counts = Counter()
    for i in incidents:
        for c in i.get("components") or extract_affected_components(i["desc_raw"]):
            if _is_real_component(c):
                counts[c] += 1
    if source["type"] == "json":
        for name in fetch_component_names(source["page_url"]):
            if _is_real_component(name):
                counts.setdefault(name, 0)
    return counts


def derive_support_patterns(incidents):
    """Derive support-URL regex patterns from links seen in incident text."""
    counts = Counter()
    for i in incidents:
        for url in re.findall(r"https?://[^\s<\"]+", i["desc_raw"]):
            url = url.rstrip(".),;")
            if re.search(r"support|help|contact|get-help", url, re.IGNORECASE):
                bare = re.sub(r"^https?://", "", url).split("?")[0].rstrip("/")
                counts[bare] += 1
    return [re.escape(u) for u, _ in counts.most_common(3)]


def heuristic_categories(components):
    """Fallback taxonomy: one category per component, keyed by its name."""
    cats = {}
    for name, _ in components.most_common(12):
        name = name.strip()
        if name:
            cats[name] = [name.lower()]
    return cats or dict(DEFAULT_COMPONENT_CATEGORIES)


def llm_categories(provider, model, components, titles):
    """Ask the LLM to group components into a taxonomy. Raises ValueError
    if the response isn't a usable {category: [keywords]} object."""
    comp_lines = "\n".join(
        f"- {name} ({count} incidents)" for name, count in components.most_common(30)
    ) or "(no component data — infer areas from the titles)"
    title_lines = "\n".join(f"- {t}" for t in titles[:40])
    prompt = _INIT_PROMPT.format(components=comp_lines, titles=title_lines)

    text = complete(provider, model, prompt, system=_INIT_SYSTEM_PROMPT).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    data = json.loads(text)

    if not isinstance(data, dict) or not data:
        raise ValueError("LLM did not return a JSON object")
    categories = {}
    for name, keywords in data.items():
        if not isinstance(name, str) or not isinstance(keywords, list):
            raise ValueError("LLM taxonomy has the wrong shape")
        kws = [str(k).strip().lower() for k in keywords if str(k).strip()]
        if kws:
            categories[name.strip()] = kws
    if not categories:
        raise ValueError("LLM taxonomy was empty")
    return categories


def render_toml(company, target_url, support_patterns, categories, taxonomy_source):
    """Render the config as TOML text (tomllib is read-only, so this is
    built by hand; json.dumps produces valid TOML basic strings)."""
    q = json.dumps
    lines = [
        f"# statuscheck config for {company} — generated by `statuscheck init`.",
        f"# Taxonomy source: {taxonomy_source}. Review and refine, then run:",
        f"#   statuscheck --config {company.lower()}.toml",
        "",
        f"company = {q(company)}",
        f"feed_url = {q(target_url)}",
        "",
    ]
    if support_patterns:
        lines.append("support_url_patterns = [")
        for p in support_patterns:
            lines.append(f"    {q(p)},")
        lines.append("]")
    else:
        lines.append("# No support/help links found in incident text; the")
        lines.append("# generic built-in patterns will be used.")
        lines.append('# support_url_patterns = ["example\\\\.com/support"]')
    lines.append("")
    lines.append("[categories]")
    for name, keywords in categories.items():
        kws = ", ".join(q(k) for k in keywords)
        lines.append(f"{q(name)} = [{kws}]")
    lines.append("")
    return "\n".join(lines)
