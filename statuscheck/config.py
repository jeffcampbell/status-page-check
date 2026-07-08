"""Optional per-company TOML configuration.

Everything works with built-in defaults; a config file refines the component
taxonomy and support-link patterns for a specific company. See
examples/config.example.toml.
"""

import tomllib

from .analysis import DEFAULT_COMPONENT_CATEGORIES
from .messaging import DEFAULT_SUPPORT_PATTERNS


def load_config(path=None):
    """Load a TOML config, merged over defaults. Returns a plain dict."""
    config = {
        "company": None,
        "feed_url": None,
        "categories": dict(DEFAULT_COMPONENT_CATEGORIES),
        "support_url_patterns": list(DEFAULT_SUPPORT_PATTERNS),
    }
    if path is None:
        return config

    with open(path, "rb") as f:
        data = tomllib.load(f)

    if data.get("company"):
        config["company"] = data["company"]
    if data.get("feed_url"):
        config["feed_url"] = data["feed_url"]
    if data.get("support_url_patterns"):
        config["support_url_patterns"] = list(data["support_url_patterns"])
    if data.get("categories"):
        # A config's taxonomy replaces the default entirely — merged keyword
        # lists would misclassify against a company-specific taxonomy.
        config["categories"] = {k: list(v) for k, v in data["categories"].items()}
    return config
