"""market.search without a credential, pinned to the real response shapes."""

import json
import unittest

from swarm import market_search
from swarm.market_search import Hit, search, search_edgar, search_hackernews


def opener_for(payload, status=200):
    body = json.dumps(payload).encode("utf-8")

    def opener(url, headers, timeout):
        return status, body

    return opener


HN_PAYLOAD = {
    "hits": [
        {
            "objectID": "41234567",
            "title": "Show HN: I built an invoicing bot",
            "url": "https://example.com/invoicing",
            "created_at": "2026-11-01T12:00:00Z",
            "story_text": "We &amp; our users <b>hate</b> late payments.",
            "points": 128,
        },
        {
            "objectID": "41234568",
            "title": "Ask HN: who pays for AI agents?",
            "url": None,
            "created_at": "2026-10-30T09:00:00Z",
            "points": 42,
        },
    ]
}

EDGAR_PAYLOAD = {
    "took": 183,
    "hits": {
        "total": {"value": 6079, "relation": "eq"},
        "hits": [
            {
                "_id": "0001047469-03-037678:a2123456.htm",
                "_score": 9.86,
                "_source": {
                    "ciks": ["0001047469"],
                    "display_names": ["ACME CORP  (CIK 0001047469)"],
                    "file_type": "8-K",
                    "file_date": "2003-12-01",
                    "period_ending": "2003-11-26",
                },
            }
        ],
    },
}


class TestHackerNews(unittest.TestCase):
    def test_parses_the_public_shape(self):
        hits = search_hackernews("invoice", opener=opener_for(HN_PAYLOAD))
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].source, "hackernews")
        self.assertEqual(hits[0].title, "Show HN: I built an invoicing bot")
        self.assertEqual(hits[0].url, "https://example.com/invoicing")
        self.assertEqual(hits[0].score, 128.0)

    def test_entities_and_tags_are_stripped_from_text(self):
        hits = search_hackernews("invoice", opener=opener_for(HN_PAYLOAD))
        self.assertEqual(hits[0].snippet, "We & our users hate late payments.")

    def test_a_missing_url_falls_back_to_the_item_page(self):
        hits = search_hackernews("invoice", opener=opener_for(HN_PAYLOAD))
        self.assertEqual(hits[1].url, "https://news.ycombinator.com/item?id=41234568")

    def test_limit_is_respected(self):
        hits = search_hackernews("invoice", limit=1, opener=opener_for(HN_PAYLOAD))
        self.assertEqual(len(hits), 1)


class TestEdgar(unittest.TestCase):
    def test_parses_the_filing_shape(self):
        hits = search_edgar("debt covenant", opener=opener_for(EDGAR_PAYLOAD))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "edgar")
        self.assertIn("8-K", hits[0].title)
        self.assertIn("ACME CORP", hits[0].title)
        self.assertEqual(hits[0].published, "2003-12-01")

    def test_archive_url_is_built_from_real_identifiers(self):
        hits = search_edgar("debt covenant", opener=opener_for(EDGAR_PAYLOAD))
        self.assertEqual(
            hits[0].url,
            "https://www.sec.gov/Archives/edgar/data/1047469/000104746903037678/a2123456.htm",
        )

    def test_no_cik_means_no_invented_link(self):
        payload = {
            "hits": {
                "hits": [
                    {"_id": "0001:x.htm", "_source": {"file_type": "8-K", "file_date": "2026-01-01"}}
                ]
            }
        }
        hits = search_edgar("anything", opener=opener_for(payload))
        self.assertEqual(hits[0].url, "")


class TestSearch(unittest.TestCase):
    def test_merges_sources_and_reports_each_one(self):
        payload = {"hits": HN_PAYLOAD["hits"]}
        report = search("invoice", sources=("hackernews",), opener=opener_for(payload))
        self.assertEqual(report["query"], "invoice")
        self.assertTrue(report["sources"]["hackernews"]["ok"])
        self.assertEqual(report["sources"]["hackernews"]["count"], 2)
        self.assertTrue(all(isinstance(h, Hit) for h in report["hits"]))

    def test_one_dead_source_does_not_hide_the_other(self):
        def opener(url, headers, timeout):
            if "hn.algolia" in url:
                return 200, json.dumps(HN_PAYLOAD).encode()
            return 503, b"upstream unavailable"

        report = search("invoice", opener=opener)
        self.assertTrue(report["sources"]["hackernews"]["ok"])
        self.assertFalse(report["sources"]["edgar"]["ok"])
        self.assertIn("503", report["sources"]["edgar"]["error"])
        self.assertEqual(len(report["hits"]), 2)

    def test_total_network_failure_is_data_not_an_exception(self):
        def opener(url, headers, timeout):
            raise OSError("no route to host")

        report = search("invoice", opener=opener)
        self.assertEqual(report["hits"], [])
        for state in report["sources"].values():
            self.assertFalse(state["ok"])
            self.assertIn("OSError", state["error"])

    def test_empty_query_is_rejected_without_a_request(self):
        called = []
        report = search(
            "   ",
            opener=lambda url, headers, timeout: called.append(url) or (200, b"{}"),
        )
        self.assertEqual(called, [])
        self.assertEqual(report["hits"], [])
        self.assertIn("empty query", report["sources"]["hackernews"]["error"])

    def test_unknown_source_is_named_not_silently_dropped(self):
        report = search("x", sources=("bing",), opener=opener_for({"hits": []}))
        self.assertFalse(report["sources"]["bing"]["ok"])
        self.assertIn("unknown source", report["sources"]["bing"]["error"])

    def test_results_are_ranked_by_score(self):
        report = search("invoice", sources=("hackernews",), opener=opener_for(HN_PAYLOAD))
        scores = [h.score for h in report["hits"]]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestProbe(unittest.TestCase):
    def test_probe_reports_live_without_a_key(self):
        result = market_search.probe(opener=opener_for(HN_PAYLOAD))
        self.assertTrue(result.ok)
        self.assertIn("keyless", result.detail)
        self.assertIsNotNone(result.latency_ms)

    def test_probe_never_raises(self):
        def opener(url, headers, timeout):
            raise OSError("offline")

        result = market_search.probe(opener=opener)
        self.assertFalse(result.ok)
        self.assertIn("OSError", result.detail)


if __name__ == "__main__":
    unittest.main()
