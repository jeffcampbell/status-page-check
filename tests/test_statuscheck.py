"""Unit tests for feed parsing, merging, analysis, and report rendering."""

import unittest

from statuscheck.analysis import (
    DEFAULT_COMPONENT_CATEGORIES,
    classify_component,
    compute_stats,
    split_periods,
)
from statuscheck.discover import is_feed_xml
from statuscheck.messaging import analyze_cadence, analyze_messaging
from statuscheck.parser import (
    extract_affected_components,
    extract_resolved_message,
    incident_key,
    merge_incidents,
    parse_feed_auto,
)
from statuscheck.report import render_report, rule_based_recommendations
from statuscheck.wayback import select_spread

RSS_INCIDENT_IO = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
<title>Acme Status</title>
<item>
<title><![CDATA[API errors on webhook delivery]]></title>
<description><![CDATA[<b>Status: Resolved</b><br/>We have resolved the issue.
Affected components:<ul><li>Webhooks (Operational)</li><li>API (Operational)</li></ul>
Visit https://acme.com/support for help.]]></description>
<content:encoded><![CDATA[<b>duplicate content to strip</b>]]></content:encoded>
<pubDate>Mon, 02 Jun 2025 14:23:00 +0000</pubDate>
<link>https://status.acme.com/incidents/abc123</link>
<guid>https://status.acme.com/incidents/abc123</guid>
</item>
<item>
<title><![CDATA[Login delays]]></title>
<description><![CDATA[<b>Status: Resolved</b><br/>Fixed.]]></description>
<pubDate>Tue, 03 Jun 2025 09:00:00 +0000</pubDate>
<link>https://status.acme.com/incidents/def456</link>
<guid>https://status.acme.com/incidents/def456</guid>
</item>
</channel>
</rss>
"""

ATOM_STATUSPAGE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Acme Status - Incident History</title>
<entry>
<title>Database outage</title>
<link href="https://status.acme.com/incidents/xyz789"/>
<id>tag:status.acme.com,2005:Incident/999</id>
<updated>2025-06-05T10:30:00Z</updated>
<content type="html">
&lt;p&gt;&lt;small&gt;Jun 5, 10:30 UTC&lt;/small&gt;&lt;br&gt;&lt;strong&gt;Resolved&lt;/strong&gt; - The database issue has been resolved.&lt;/p&gt;
&lt;p&gt;&lt;small&gt;Jun 5, 09:15 UTC&lt;/small&gt;&lt;br&gt;&lt;strong&gt;Investigating&lt;/strong&gt; - We are investigating database errors.&lt;/p&gt;
</content>
</entry>
</feed>
"""


class TestParser(unittest.TestCase):
    def test_parse_rss_incident_io(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO)
        self.assertEqual(len(incidents), 2)
        first = incidents[0]
        self.assertEqual(first["title"], "API errors on webhook delivery")
        self.assertIn("We have resolved", first["desc_text"])
        self.assertNotIn("duplicate content", first["desc_text"])
        self.assertEqual(first["pub_date"].year, 2025)
        self.assertEqual(first["link"], "https://status.acme.com/incidents/abc123")

    def test_parse_atom_statuspage(self):
        incidents = parse_feed_auto(ATOM_STATUSPAGE)
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc["title"], "Database outage")
        self.assertEqual(inc["pub_date"].hour, 10)
        self.assertEqual(inc["link"], "https://status.acme.com/incidents/xyz789")

    def test_extract_components(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO)
        comps = extract_affected_components(incidents[0]["desc_raw"])
        self.assertEqual(comps, ["Webhooks", "API"])

    def test_extract_resolved_message_atom_timeline(self):
        incidents = parse_feed_auto(ATOM_STATUSPAGE)
        msg = extract_resolved_message(incidents[0]["desc_raw"])
        self.assertIn("database issue has been resolved", msg)
        self.assertNotIn("Investigating", msg)

    def test_extract_resolved_message_rss(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO)
        msg = extract_resolved_message(incidents[1]["desc_raw"])
        self.assertEqual(msg, "Fixed.")

    def test_merge_dedupes_by_link(self):
        a = parse_feed_auto(RSS_INCIDENT_IO)
        b = parse_feed_auto(RSS_INCIDENT_IO)
        b[0]["desc_text"] = "older snapshot version"
        merged = merge_incidents(a, b)
        self.assertEqual(len(merged), 2)
        # The first list's version wins
        self.assertIn("We have resolved", merged[0]["desc_text"])

    def test_incident_key_fallback_without_link(self):
        inc = {"title": "Some Incident", "link": "", "guid": "", "pub_date": None}
        self.assertIn("some incident", incident_key(inc))


class TestDiscover(unittest.TestCase):
    def test_is_feed_xml(self):
        self.assertTrue(is_feed_xml(RSS_INCIDENT_IO))
        self.assertTrue(is_feed_xml(ATOM_STATUSPAGE))
        self.assertFalse(is_feed_xml("<!DOCTYPE html><html><body>hi</body></html>"))


class TestWayback(unittest.TestCase):
    def test_select_spread(self):
        ts = [str(i) for i in range(100)]
        picked = select_spread(ts, 5)
        self.assertEqual(len(picked), 5)
        self.assertEqual(picked[0], "0")
        self.assertEqual(picked[-1], "99")
        self.assertEqual(select_spread(["a", "b"], 5), ["a", "b"])


class TestAnalysis(unittest.TestCase):
    def test_classify(self):
        cat = classify_component(
            "API errors on webhook delivery", "", DEFAULT_COMPONENT_CATEGORIES
        )
        self.assertEqual(cat, "API")
        self.assertEqual(classify_component("zzz", "zzz", {}), "Other")

    def test_compute_stats(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO)
        stats = compute_stats(incidents, "test", DEFAULT_COMPONENT_CATEGORIES)
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["date_start"], "2025-06-02")
        self.assertGreater(stats["per_week"], 0)

    def test_split_periods_too_short(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO)
        self.assertIsNone(split_periods(incidents))


class TestMessagingAndReport(unittest.TestCase):
    def _build(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO) + parse_feed_auto(ATOM_STATUSPAGE)
        stats = compute_stats(incidents, "All data", DEFAULT_COMPONENT_CATEGORIES)
        messaging = analyze_messaging(incidents)
        cadence = analyze_cadence(incidents)
        return stats, messaging, cadence

    def test_analyze_messaging(self):
        _, messaging, _ = self._build()
        self.assertEqual(messaging["count"], 3)
        checks = {s["check"]: s for s in messaging["structure"]}
        self.assertEqual(checks["Support link included"]["count"], 1)
        # "Fixed." is under 30 chars → terse-resolution quality issue
        self.assertTrue(
            any("terse" in issue for _, issue in messaging["quality_issues"])
        )

    def test_custom_support_patterns_compose_case_insensitively(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO)
        # Multiple patterns (one with a legacy (?i) prefix) must not raise on
        # Python 3.11+ and must match case-insensitively
        messaging = analyze_messaging(
            incidents, [r"(?i)ACME\.com/support", r"support\.acme\.com"]
        )
        checks = {s["check"]: s for s in messaging["structure"]}
        self.assertEqual(checks["Support link included"]["count"], 1)

    def test_analyze_cadence(self):
        _, _, cadence = self._build()
        self.assertEqual(cadence["dated_count"], 3)
        # 09:00 and 10:30 hit :00/:30
        self.assertEqual(cadence["round_minute_count"], 2)

    def test_rule_based_recommendations(self):
        _, messaging, cadence = self._build()
        recs = rule_based_recommendations(messaging, cadence)
        self.assertTrue(any("support-link" in r for r in recs))

    def test_render_report_without_llm(self):
        stats, messaging, cadence = self._build()
        md = render_report(
            company="Acme",
            stats_all=stats,
            periods=[stats],
            messaging=messaging,
            period_messaging=[messaging],
            cadence=cadence,
            llm_sections={},
            meta={"feed_url": "https://status.acme.com/feed.rss", "snapshots_used": 0},
        )
        self.assertIn("# Acme Incident Management", md)
        self.assertIn("## Executive Summary", md)
        self.assertIn("## 3. Recommendations", md)
        self.assertIn("Appendix B", md)
        # No LLM → no templates appendix
        self.assertNotIn("Appendix A", md)


if __name__ == "__main__":
    unittest.main()
