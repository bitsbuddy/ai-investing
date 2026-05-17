from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from ai_investing.models import OfficialNewsContext, OfficialNewsItem, ResearchAsset
from ai_investing.news import build_official_news_context, score_official_news_for_asset


class OfficialNewsTests(unittest.TestCase):
    def test_context_builds_from_official_source_payloads(self) -> None:
        fed_xml = """<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Federal Reserve issues FOMC statement</title>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm</link>
    <description>The Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent.</description>
    <pubDate>Wed, 29 Apr 2026 18:00:00 GMT</pubDate>
  </item>
</channel></rss>"""
        bls_html = """
<html><body>
<section>Economic Releases 05/13/2026 PPI for final demand advances 1.4% in April; services rise 1.2%, goods increase 2.0%
05/12/2026 CPI for all items rises 0.6% in April; shelter and gasoline up
All Releases</section>
</body></html>
"""
        treasury_html = """
<html><body>
<div>Press Releases May 4, 2026 Treasury Announces Marketable Borrowing Estimates
Pagination</div>
</body></html>
"""
        sec_tickers = {
            "0": {"ticker": "MSFT", "cik_str": 789019, "title": "MICROSOFT CORP"}
        }
        sec_submissions = {
            "cik": "789019",
            "filings": {
                "recent": {
                    "form": ["10-Q", "8-K"],
                    "filingDate": ["2026-05-10", "2026-05-08"],
                    "accessionNumber": ["0000789019-26-000010", "0000789019-26-000009"],
                    "primaryDocument": ["msft10q.htm", "msft8k.htm"],
                }
            },
        }

        def fake_fetch(url: str, *, user_agent: str, timeout_seconds: int = 15) -> str:
            if "press_all.xml" in url:
                return fed_xml
            if "bls.gov/home.htm" in url:
                return bls_html
            if "treasury.gov/news/press-releases" in url:
                return treasury_html
            if "company_tickers.json" in url:
                return __import__("json").dumps(sec_tickers)
            if "CIK0000789019.json" in url:
                return __import__("json").dumps(sec_submissions)
            raise AssertionError(f"Unexpected URL: {url}")

        assets = {
            "MSFT": ResearchAsset(symbol="MSFT", asset_type="equity"),
            "SPY": ResearchAsset(symbol="SPY", asset_type="etf", benchmark_index="SPX"),
        }
        with patch("ai_investing.news._fetch_text", side_effect=fake_fetch), patch(
            "ai_investing.news.time.sleep"
        ):
            context = build_official_news_context(
                as_of=date(2026, 5, 17),
                lookback_days=14,
                user_agent="AI-Investing tests@example.com",
                assets=assets,
                require_success=True,
            )

        assert context is not None
        self.assertIn("fed", context.source_status)
        self.assertIn("bls", context.source_status)
        self.assertIn("treasury", context.source_status)
        self.assertIn("sec", context.source_status)
        self.assertGreater(len(context.items), 0)
        self.assertIn("MSFT", context.company_scores)
        self.assertLess(context.risk_on_score or 0.0, 0.55)

    def test_score_for_asset_blends_macro_and_company_signals(self) -> None:
        context = OfficialNewsContext(
            as_of=date(2026, 5, 17),
            lookback_days=14,
            risk_on_score=0.40,
            duration_score=0.65,
            cash_score=0.55,
            gold_score=0.60,
            company_scores={"MSFT": 0.70},
            items=(
                OfficialNewsItem(
                    source="fed",
                    published_on=date(2026, 5, 16),
                    title="Federal Reserve issues FOMC statement",
                    url="https://www.federalreserve.gov/",
                    impact_scores={"risk_on": 0.40},
                ),
            ),
        )

        msft_score = score_official_news_for_asset(
            context,
            symbol="MSFT",
            asset_type="equity",
            benchmark_index="NDX",
        )
        tlt_score = score_official_news_for_asset(
            context,
            symbol="TLT",
            asset_type="etf",
            benchmark_index="UST20",
        )

        assert msft_score is not None
        assert tlt_score is not None
        self.assertGreater(msft_score, context.risk_on_score or 0.0)
        self.assertEqual(tlt_score, context.duration_score)


if __name__ == "__main__":
    unittest.main()
