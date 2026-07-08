"""Unit tests for feed parsing, merging, analysis, and report rendering."""

import tomllib
import unittest
from datetime import datetime, timezone

from statuscheck.analysis import (
    DEFAULT_COMPONENT_CATEGORIES,
    analyze_transparency,
    classify_component,
    compute_stats,
    filter_focus,
    split_periods,
)
from statuscheck.discover import is_feed_xml
from statuscheck.htmlout import render_html
from statuscheck.initcfg import (
    derive_support_patterns,
    heuristic_categories,
    render_toml,
)
from statuscheck.jsonapi import parse_statuspage_json
from statuscheck.lifecycle import analyze_lifecycle, disclosed_downtime, fmt_minutes
from statuscheck.llm import prompts_for_mode
from statuscheck.messaging import analyze_cadence, analyze_messaging
from statuscheck.parser import (
    extract_affected_components,
    extract_resolved_message,
    extract_timed_updates,
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

    def test_incident_key_collapses_double_slashes(self):
        # incident.io RSS uses host//incidents/<id>; the JSON API view of
        # the same incident uses a single slash — keys must match
        a = {"link": "https://status.acme.com//incidents/ABC", "guid": "", "pub_date": None, "title": "x"}
        b = {"link": "https://status.acme.com/incidents/ABC", "guid": "", "pub_date": None, "title": "x"}
        self.assertEqual(incident_key(a), incident_key(b))
        self.assertTrue(incident_key(a).startswith("https://"))

    def test_incident_key_fallback_without_link(self):
        inc = {"title": "Some Incident", "link": "", "guid": "", "pub_date": None}
        self.assertIn("some incident", incident_key(inc))


STATUSPAGE_JSON = {
    "page": {"url": "https://www.acmestatus.com"},
    "incidents": [
        {
            "id": "abc123",
            "name": "Database outage",
            "impact": "major",
            "created_at": "2026-06-05T09:15:00.000Z",
            "started_at": "2026-06-05T09:10:00.000Z",
            "resolved_at": "2026-06-05T10:30:00.000Z",
            "shortlink": "https://stspg.io/xyz",
            "components": [{"name": "Database"}, {"name": "API"}],
            "incident_updates": [
                {
                    "status": "resolved",
                    "body": "The database issue has been resolved.",
                    "created_at": "2026-06-05T10:30:00.000Z",
                    "display_at": "2026-06-05T10:30:00.000Z",
                },
                {
                    "status": "investigating",
                    "body": "We are investigating database errors.",
                    "created_at": "2026-06-05T09:15:00.000Z",
                    "display_at": "2026-06-05T09:15:00.000Z",
                },
            ],
        },
    ],
}


class TestStatuspageJson(unittest.TestCase):
    def test_normalization(self):
        page_url, incidents = parse_statuspage_json(STATUSPAGE_JSON, "https://fallback")
        self.assertEqual(page_url, "https://www.acmestatus.com")
        inc = incidents[0]
        self.assertEqual(inc["title"], "Database outage")
        self.assertEqual(inc["impact"], "major")
        self.assertEqual(inc["link"], "https://www.acmestatus.com/incidents/abc123")
        self.assertEqual(inc["components"], ["Database", "API"])
        # updates_timed is sorted oldest-first
        self.assertEqual(inc["updates_timed"][0]["status"], "Investigating")
        self.assertEqual(inc["updates_timed"][-1]["status"], "Resolved")
        # desc_raw mirrors the Atom feed shape, so existing regexes work
        self.assertIn(
            "database issue has been resolved",
            extract_resolved_message(inc["desc_raw"]),
        )
        # duration start prefers started_at over created_at
        self.assertEqual(inc["created_at"].minute, 10)

    def test_flows_into_stats(self):
        _, incidents = parse_statuspage_json(STATUSPAGE_JSON, "https://fallback")
        stats = compute_stats(incidents, "t", DEFAULT_COMPONENT_CATEGORIES)
        self.assertEqual(stats["severity"]["major"], 1)
        self.assertEqual(stats["affected"]["Database"], 1)
        # Component tags participate in classification even though they
        # don't appear in the description text
        self.assertIn(incidents[0]["category"], ("Data / Storage", "API"))


class TestTimedUpdates(unittest.TestCase):
    # Real Statuspage Atom markup, including <var> wrappers and no year
    ATOM_HTML = (
        "<p> <small>Jul <var data-var='date'> 7</var>, "
        "<var data-var='time'>16:17</var> UTC</small><br> "
        "<strong>Resolved</strong> - Fixed. </p>"
        "<p> <small>Jul <var data-var='date'> 7</var>, "
        "<var data-var='time'>14:02</var> UTC</small><br> "
        "<strong>Investigating</strong> - Looking into it. </p>"
    )

    def test_extract_with_var_tags(self):
        ref = datetime(2026, 7, 8, tzinfo=timezone.utc)
        ups = extract_timed_updates(self.ATOM_HTML, ref)
        self.assertEqual(len(ups), 2)
        self.assertEqual(ups[0]["status"], "Investigating")  # oldest first
        self.assertEqual(ups[-1]["at"].hour, 16)
        self.assertEqual(ups[-1]["at"].year, 2026)

    def test_year_inference_across_boundary(self):
        html = (
            "<p><small>Dec 31, 23:50 UTC</small><br>"
            "<strong>Resolved</strong> - Done.</p>"
        )
        ref = datetime(2026, 1, 2, tzinfo=timezone.utc)
        ups = extract_timed_updates(html, ref)
        self.assertEqual(ups[0]["at"].year, 2025)

    def test_unknown_timezone_skipped(self):
        html = (
            "<p><small>Jul 7, 16:17 XYZ</small><br>"
            "<strong>Resolved</strong> - Done.</p>"
        )
        ref = datetime(2026, 7, 8, tzinfo=timezone.utc)
        self.assertEqual(extract_timed_updates(html, ref), [])


class TestLifecycle(unittest.TestCase):
    def _incident(self, start_h, end_h, title="inc"):
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return {
            "title": title,
            "desc_raw": "",
            "pub_date": base.replace(hour=start_h),
            "created_at": base.replace(hour=start_h),
            "resolved_at": base.replace(hour=end_h),
            "updates_timed": [
                {"status": "Investigating", "at": base.replace(hour=start_h)},
                {"status": "Resolved", "at": base.replace(hour=end_h)},
            ],
        }

    def test_metrics(self):
        incidents = [
            self._incident(1, 2),   # 60 min
            self._incident(3, 5),   # 120 min
            self._incident(6, 12),  # 360 min
        ]
        lc = analyze_lifecycle(incidents)
        self.assertEqual(lc["covered"], 3)
        self.assertEqual(lc["median_minutes"], 120)
        self.assertAlmostEqual(lc["within"]["1h"], 100 / 3)
        self.assertEqual(lc["longest"][0]["minutes"], 360)
        self.assertEqual(lc["gap_median_minutes"], 120)

    def test_too_few_returns_none(self):
        self.assertIsNone(analyze_lifecycle([self._incident(1, 2)]))

    def test_fmt_minutes(self):
        self.assertEqual(fmt_minutes(47), "47 min")
        self.assertEqual(fmt_minutes(185), "3h 05m")
        self.assertEqual(fmt_minutes(60 * 28), "1d 4h")


class TestFocusAndTransparency(unittest.TestCase):
    def _incidents(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO) + parse_feed_auto(ATOM_STATUSPAGE)
        compute_stats(incidents, "t", DEFAULT_COMPONENT_CATEGORIES)  # assigns categories
        return incidents

    def test_filter_focus_by_component(self):
        matched = filter_focus(self._incidents(), ["webhooks"])
        self.assertEqual(len(matched), 1)
        self.assertIn("webhook", matched[0]["title"].lower())

    def test_filter_focus_by_title(self):
        matched = filter_focus(self._incidents(), ["login"])
        self.assertEqual([m["title"] for m in matched], ["Login delays"])

    def test_filter_focus_empty_terms_passthrough(self):
        incidents = self._incidents()
        self.assertEqual(filter_focus(incidents, [" "]), incidents)

    def test_transparency_never_above_minor(self):
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        incidents = [
            {"title": f"i{k}", "desc_raw": "x", "pub_date": base,
             "created_at": base, "impact": "minor"}
            for k in range(4)
        ]
        t = analyze_transparency(incidents)
        self.assertTrue(t["never_above_minor"])
        self.assertEqual(t["severity_coverage_pct"], 100)
        self.assertEqual(t["major_count"], 0)

    def test_transparency_backfill_detection(self):
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        late = {
            "title": "backfilled", "desc_raw": "x", "impact": "major",
            "created_at": base, "pub_date": base.replace(day=3),  # posted 2 days late
        }
        ontime = {
            "title": "prompt", "desc_raw": "root cause was found. postmortem to follow",
            "impact": "major", "created_at": base, "pub_date": base,
        }
        t = analyze_transparency([late, ontime])
        self.assertEqual(len(t["backfilled"]), 1)
        self.assertEqual(t["backfilled"][0][0], "backfilled")
        self.assertEqual(t["postmortem_rate_pct"], 50)


class TestDisclosedDowntime(unittest.TestCase):
    def _incident(self, day, hours, impact):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(day=day)
        return {
            "title": "x", "desc_raw": "", "pub_date": base,
            "created_at": base,
            "resolved_at": base.replace(hour=hours),
            "impact": impact,
        }

    def test_major_critical_basis(self):
        # 40-day window (Jan 1 → Feb 10 via day arithmetic won't work; use two months)
        incidents = [
            self._incident(1, 12, "major"),    # 12h
            self._incident(15, 6, "critical"), # 6h
            self._incident(20, 10, "minor"),   # excluded from pool
        ]
        # stretch the window past 30 days
        incidents.append(self._incident(1, 1, "minor"))
        incidents[-1]["pub_date"] = incidents[-1]["pub_date"].replace(month=2, day=15)
        incidents[-1]["created_at"] = incidents[-1]["pub_date"]
        incidents[-1]["resolved_at"] = incidents[-1]["pub_date"].replace(hour=1)

        d = disclosed_downtime(incidents)
        self.assertEqual(d["basis"], "major/critical incidents")
        self.assertEqual(d["incident_count"], 2)
        self.assertEqual(d["total_minutes"], 18 * 60)
        self.assertEqual(d["window_days"], 45)
        self.assertLess(d["availability_pct"], 100)

    def test_no_severity_falls_back_to_all(self):
        incidents = [self._incident(1, 2, None), self._incident(15, 3, None)]
        incidents.append(self._incident(1, 1, None))
        incidents[-1]["pub_date"] = incidents[-1]["pub_date"].replace(month=3)
        incidents[-1]["created_at"] = incidents[-1]["pub_date"]
        incidents[-1]["resolved_at"] = incidents[-1]["pub_date"].replace(hour=1)
        d = disclosed_downtime(incidents)
        self.assertEqual(d["basis"], "all incidents with computable duration")
        self.assertEqual(d["incident_count"], 3)

    def test_short_window_returns_none(self):
        self.assertIsNone(
            disclosed_downtime([self._incident(1, 2, "major"), self._incident(2, 3, "major")])
        )


class TestModes(unittest.TestCase):
    def test_vendor_prompts_drop_templates(self):
        neutral = prompts_for_mode("neutral")
        vendor = prompts_for_mode("vendor")
        self.assertIn("templates", neutral)
        self.assertNotIn("templates", vendor)
        self.assertNotEqual(neutral["recommendations"], vendor["recommendations"])
        self.assertIn("risks", vendor["recommendations"])

    def _render(self, mode):
        incidents = parse_feed_auto(RSS_INCIDENT_IO) + parse_feed_auto(ATOM_STATUSPAGE)
        stats = compute_stats(incidents, "All data", DEFAULT_COMPONENT_CATEGORIES)
        messaging = analyze_messaging(incidents)
        cadence = analyze_cadence(incidents)
        return render_report(
            company="Acme",
            stats_all=stats,
            periods=[stats],
            messaging=messaging,
            period_messaging=[messaging],
            cadence=cadence,
            llm_sections={},
            meta={"source_label": "test", "snapshots_used": 0},
            transparency=analyze_transparency(incidents),
            mode=mode,
        )

    def test_vendor_mode_report(self):
        md = self._render("vendor")
        self.assertIn("Vendor Reliability & Transparency Assessment", md)
        self.assertIn("## 3. Risk Assessment & Questions for the Vendor", md)
        self.assertNotIn("## 3. Recommendations", md)

    def test_self_mode_report(self):
        md = self._render("self")
        self.assertIn("Internal Audit", md)
        self.assertIn("### 2.8 Exhibits", md)
        self.assertIn("## 3. Recommendations", md)

    def test_neutral_mode_unchanged(self):
        md = self._render("neutral")
        self.assertIn("External Assessment", md)
        self.assertNotIn("Exhibits", md)


class TestHtmlOut(unittest.TestCase):
    MD = (
        "# Acme Report\n\n*Generated today.*\n\n## Summary\n\n"
        "Some **bold** text with a [link](https://x.com) and <script>alert(1)</script>.\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "- item one\n  - nested\n- item two\n\n"
        "```\nbar chart ███\n```\n\n### Detail\n\n> a caveat\n"
    )

    def test_render(self):
        html = render_html(self.MD, title="Acme <Test>")
        self.assertIn("<title>Acme &lt;Test&gt;</title>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn('<a href="https://x.com">link</a>', html)
        self.assertNotIn("<script>", html)  # escaped, not executable
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<th>A</th>", html)
        self.assertIn("<td>1</td>", html)
        self.assertIn("<li>nested</li>", html)
        self.assertIn("bar chart ███", html)
        self.assertIn("<blockquote>", html)
        # TOC links to both headings
        self.assertIn('class="toc"', html)
        self.assertIn('href="#s1"', html)
        self.assertIn('href="#s2"', html)


class TestInitCfg(unittest.TestCase):
    def test_derive_support_patterns(self):
        incidents = parse_feed_auto(RSS_INCIDENT_IO)
        patterns = derive_support_patterns(incidents)
        self.assertEqual(patterns, ["acme\\.com/support"])

    def test_render_toml_round_trips(self):
        from collections import Counter

        cats = heuristic_categories(Counter({"Webhooks": 5, "API": 3}))
        toml_text = render_toml(
            "Acme",
            "https://status.acme.com",
            ["acme\\.com/support"],
            cats,
            "heuristic",
        )
        data = tomllib.loads(toml_text)
        self.assertEqual(data["company"], "Acme")
        self.assertEqual(data["support_url_patterns"], ["acme\\.com/support"])
        self.assertEqual(data["categories"]["Webhooks"], ["webhooks"])


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
            meta={
                "source_label": "RSS/Atom feed (https://status.acme.com/feed.rss)",
                "snapshots_used": 0,
            },
        )
        self.assertIn("# Acme: Incident Management — External Assessment", md)
        self.assertIn("## Executive Summary", md)
        self.assertIn("## 3. Recommendations", md)
        self.assertIn("Appendix B", md)
        # No LLM → no templates appendix
        self.assertNotIn("Appendix A", md)


if __name__ == "__main__":
    unittest.main()
