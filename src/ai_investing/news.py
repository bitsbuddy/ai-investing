from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .models import OfficialNewsContext, OfficialNewsItem, ResearchAsset

_FED_PRESS_FEED_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
_BLS_HOME_URL = "https://www.bls.gov/home.htm"
_TREASURY_PRESS_URL = "https://home.treasury.gov/news/press-releases?page=0"
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

_RISK_BUCKET = "risk_on"
_DURATION_BUCKET = "duration"
_CASH_BUCKET = "cash"
_GOLD_BUCKET = "gold"


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self._parts)


def build_official_news_context(
    *,
    as_of: date,
    lookback_days: int,
    user_agent: str,
    assets: dict[str, ResearchAsset] | None = None,
    require_success: bool = False,
) -> OfficialNewsContext | None:
    statuses: dict[str, str] = {}
    items: list[OfficialNewsItem] = []
    company_scores: dict[str, float] = {}

    fetchers = (
        ("fed", lambda: _fetch_fed_items(as_of, lookback_days, user_agent)),
        ("bls", lambda: _fetch_bls_items(as_of, lookback_days, user_agent)),
        ("treasury", lambda: _fetch_treasury_items(as_of, lookback_days, user_agent)),
    )
    for source_name, fetcher in fetchers:
        try:
            source_items = fetcher()
        except RuntimeError as exc:
            statuses[source_name] = f"error: {exc}"
            continue
        items.extend(source_items)
        statuses[source_name] = f"ok ({len(source_items)} items)"

    company_symbols = _company_symbols_from_assets(assets or {})
    if company_symbols:
        try:
            company_scores, sec_items = _fetch_sec_company_items(
                company_symbols,
                as_of=as_of,
                lookback_days=lookback_days,
                user_agent=user_agent,
            )
        except RuntimeError as exc:
            statuses["sec"] = f"error: {exc}"
        else:
            items.extend(sec_items)
            statuses["sec"] = f"ok ({len(company_scores)} symbols)"
    else:
        statuses["sec"] = "skipped (no equity assets in snapshot)"

    items.sort(key=lambda item: (item.published_on, item.source, item.title), reverse=True)
    risk_on_score = _aggregate_bucket_score(items, _RISK_BUCKET, as_of)
    duration_score = _aggregate_bucket_score(items, _DURATION_BUCKET, as_of)
    cash_score = _aggregate_bucket_score(items, _CASH_BUCKET, as_of)
    gold_score = _aggregate_bucket_score(items, _GOLD_BUCKET, as_of)

    if not items and not company_scores:
        if require_success:
            raise RuntimeError(
                "Official-source news fetch produced no usable data from Fed, BLS, Treasury, or SEC."
            )
        return None

    return OfficialNewsContext(
        as_of=as_of,
        lookback_days=lookback_days,
        risk_on_score=risk_on_score,
        duration_score=duration_score,
        cash_score=cash_score,
        gold_score=gold_score,
        company_scores=company_scores,
        items=tuple(items),
        source_status=statuses,
    )


def score_official_news_for_asset(
    context: OfficialNewsContext,
    *,
    symbol: str,
    asset_type: str | None,
    benchmark_index: str | None,
) -> float | None:
    symbol = symbol.upper()
    bucket = _bucket_for_asset(symbol, asset_type, benchmark_index)
    bucket_score = None
    if bucket == _RISK_BUCKET:
        bucket_score = context.risk_on_score
    elif bucket == _DURATION_BUCKET:
        bucket_score = context.duration_score
    elif bucket == _CASH_BUCKET:
        bucket_score = context.cash_score
    elif bucket == _GOLD_BUCKET:
        bucket_score = context.gold_score

    company_score = context.company_scores.get(symbol)
    values: list[tuple[float, float]] = []
    if bucket_score is not None:
        values.append((bucket_score, 0.60 if company_score is not None else 1.0))
    if company_score is not None:
        values.append((company_score, 0.40 if bucket_score is not None else 1.0))
    if not values:
        return None
    total_weight = sum(weight for _, weight in values)
    return sum(value * weight for value, weight in values) / total_weight


def summarize_official_news(
    context: OfficialNewsContext, *, limit: int = 5
) -> list[str]:
    lines = []
    for item in context.items[:limit]:
        lines.append(
            f"{item.published_on.isoformat()} | {item.source.upper()} | {item.title}"
        )
    return lines


def _fetch_fed_items(as_of: date, lookback_days: int, user_agent: str) -> list[OfficialNewsItem]:
    xml_text = _fetch_text(_FED_PRESS_FEED_URL, user_agent=user_agent)
    return _parse_fed_press_feed(xml_text, as_of=as_of, lookback_days=lookback_days)


def _fetch_bls_items(as_of: date, lookback_days: int, user_agent: str) -> list[OfficialNewsItem]:
    html = _fetch_text(_BLS_HOME_URL, user_agent=user_agent)
    return _parse_bls_homepage(html, as_of=as_of, lookback_days=lookback_days)


def _fetch_treasury_items(
    as_of: date, lookback_days: int, user_agent: str
) -> list[OfficialNewsItem]:
    html = _fetch_text(_TREASURY_PRESS_URL, user_agent=user_agent)
    return _parse_treasury_press_page(html, as_of=as_of, lookback_days=lookback_days)


def _fetch_sec_company_items(
    symbols: Iterable[str],
    *,
    as_of: date,
    lookback_days: int,
    user_agent: str,
) -> tuple[dict[str, float], list[OfficialNewsItem]]:
    ticker_payload = json.loads(_fetch_text(_SEC_TICKERS_URL, user_agent=user_agent))
    cik_by_symbol = _parse_sec_ticker_map(ticker_payload)
    scores: dict[str, float] = {}
    items: list[OfficialNewsItem] = []
    for symbol in sorted({value.upper() for value in symbols}):
        cik = cik_by_symbol.get(symbol)
        if cik is None:
            continue
        time.sleep(0.12)
        payload = json.loads(
            _fetch_text(
                _SEC_SUBMISSIONS_URL.format(cik=f"{cik:010d}"),
                user_agent=user_agent,
            )
        )
        symbol_score, symbol_items = _parse_sec_submissions(
            payload,
            symbol=symbol,
            as_of=as_of,
            lookback_days=lookback_days,
        )
        if symbol_score is not None:
            scores[symbol] = symbol_score
        items.extend(symbol_items)
    return scores, items


def _fetch_text(url: str, *, user_agent: str, timeout_seconds: int = 15) -> str:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"unable to fetch {url}: {exc}") from exc


def _parse_fed_press_feed(
    xml_text: str, *, as_of: date, lookback_days: int
) -> list[OfficialNewsItem]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"invalid Federal Reserve feed XML: {exc}") from exc

    minimum_date = as_of - timedelta(days=lookback_days)
    items: list[OfficialNewsItem] = []
    for item in root.findall(".//item"):
        title = _clean_text(item.findtext("title"))
        link = _clean_text(item.findtext("link"))
        summary = _clean_text(item.findtext("description"))
        published_on = _parse_any_date(item.findtext("pubDate"))
        if not title or not link or published_on is None or published_on < minimum_date:
            continue
        scores = _score_fed_release(title, summary)
        if not scores:
            continue
        items.append(
            OfficialNewsItem(
                source="fed",
                published_on=published_on,
                title=title,
                url=link,
                impact_scores=scores,
                summary=summary or None,
            )
        )
    return items


def _parse_bls_homepage(
    html: str, *, as_of: date, lookback_days: int
) -> list[OfficialNewsItem]:
    text = _html_to_text(html)
    minimum_date = as_of - timedelta(days=lookback_days)
    section_start = text.find("Economic Releases")
    if section_start >= 0:
        text = text[section_start:]
    pattern = re.compile(
        r"(\d{2}/\d{2}/\d{4})\s+(.+?)(?=\s+\d{2}/\d{2}/\d{4}\s+|\s+All Releases\b|\s+Economic news release finder\b)",
        re.IGNORECASE,
    )
    items: list[OfficialNewsItem] = []
    for raw_date, raw_title in pattern.findall(text):
        published_on = _parse_any_date(raw_date)
        if published_on is None or published_on < minimum_date:
            continue
        title = _clean_text(raw_title)
        scores = _score_bls_release(title)
        if not scores:
            continue
        items.append(
            OfficialNewsItem(
                source="bls",
                published_on=published_on,
                title=title,
                url=_BLS_HOME_URL,
                impact_scores=scores,
            )
        )
    return items


def _parse_treasury_press_page(
    html: str, *, as_of: date, lookback_days: int
) -> list[OfficialNewsItem]:
    text = _html_to_text(html)
    minimum_date = as_of - timedelta(days=lookback_days)
    section_start = text.find("Press Releases")
    if section_start >= 0:
        text = text[section_start:]
    pattern = re.compile(
        r"([A-Z][a-z]+ \d{1,2}, \d{4})(?:\s+[A-Za-z&]+)?\s+(.+?)(?=\s+[A-Z][a-z]+ \d{1,2}, \d{4}(?:\s+[A-Za-z&]+)?\s+|\s+Pagination\b|\s+Keyword Search\b)",
        re.IGNORECASE,
    )
    items: list[OfficialNewsItem] = []
    for raw_date, raw_title in pattern.findall(text):
        published_on = _parse_any_date(raw_date)
        if published_on is None or published_on < minimum_date:
            continue
        title = _clean_text(raw_title)
        scores = _score_treasury_release(title)
        if not scores:
            continue
        items.append(
            OfficialNewsItem(
                source="treasury",
                published_on=published_on,
                title=title,
                url=_TREASURY_PRESS_URL,
                impact_scores=scores,
            )
        )
    return items


def _parse_sec_ticker_map(payload: dict) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for value in payload.values():
        ticker = str(value.get("ticker", "")).upper()
        cik = value.get("cik_str")
        if not ticker or cik is None:
            continue
        mapping[ticker] = int(cik)
    return mapping


def _parse_sec_submissions(
    payload: dict,
    *,
    symbol: str,
    as_of: date,
    lookback_days: int,
) -> tuple[float | None, list[OfficialNewsItem]]:
    recent = payload.get("filings", {}).get("recent", {})
    forms = list(recent.get("form", []))
    filing_dates = list(recent.get("filingDate", []))
    accessions = list(recent.get("accessionNumber", []))
    primary_documents = list(recent.get("primaryDocument", []))
    cik = int(payload.get("cik", 0))
    minimum_date = as_of - timedelta(days=lookback_days)
    items: list[OfficialNewsItem] = []
    weighted_values: list[tuple[float, float]] = []

    for form, raw_date, accession, primary_document in zip(
        forms, filing_dates, accessions, primary_documents, strict=False
    ):
        filed_on = _parse_any_date(raw_date)
        if filed_on is None or filed_on < minimum_date:
            continue
        score = _score_sec_form(str(form))
        if score is None:
            continue
        title = f"{symbol} filed {form}"
        accession_no_dashes = str(accession).replace("-", "")
        if cik and accession_no_dashes and primary_document:
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                f"{accession_no_dashes}/{primary_document}"
            )
        else:
            url = "https://www.sec.gov/search-filings"
        items.append(
            OfficialNewsItem(
                source="sec",
                published_on=filed_on,
                title=title,
                url=url,
                impact_scores={_RISK_BUCKET: score},
                symbols=(symbol,),
            )
        )
        weighted_values.append((score, _recency_weight(filed_on, as_of)))

    if not weighted_values:
        return None, items
    total_weight = sum(weight for _, weight in weighted_values)
    symbol_score = sum(value * weight for value, weight in weighted_values) / total_weight
    return symbol_score, items


def _score_fed_release(title: str, summary: str) -> dict[str, float] | None:
    text = f"{title} {summary}".lower()
    if not any(
        keyword in text
        for keyword in ("fomc", "monetary policy", "implementation note", "balance sheet")
    ):
        return None
    if any(keyword in text for keyword in ("lower the target range", "cut", "reduce rates", "easing")):
        return {
            _RISK_BUCKET: 0.68,
            _DURATION_BUCKET: 0.72,
            _CASH_BUCKET: 0.40,
            _GOLD_BUCKET: 0.58,
        }
    if any(keyword in text for keyword in ("raise the target range", "increase rates", "higher for longer", "tighten")):
        return {
            _RISK_BUCKET: 0.32,
            _DURATION_BUCKET: 0.24,
            _CASH_BUCKET: 0.68,
            _GOLD_BUCKET: 0.58,
        }
    if any(keyword in text for keyword in ("maintain the target range", "unchanged", "maintain the interest rate")):
        return {
            _RISK_BUCKET: 0.50,
            _DURATION_BUCKET: 0.48,
            _CASH_BUCKET: 0.52,
            _GOLD_BUCKET: 0.52,
        }
    return {
        _RISK_BUCKET: 0.48,
        _DURATION_BUCKET: 0.48,
        _CASH_BUCKET: 0.52,
        _GOLD_BUCKET: 0.52,
    }


def _score_bls_release(title: str) -> dict[str, float] | None:
    text = title.lower()
    inflation_release = any(
        keyword in text
        for keyword in ("cpi", "consumer price index", "ppi", "producer price index", "import prices", "export prices")
    )
    if inflation_release:
        if any(keyword in text for keyword in ("rises", "rose", "up", "advances", "increase", "higher")):
            return {
                _RISK_BUCKET: 0.38,
                _DURATION_BUCKET: 0.28,
                _CASH_BUCKET: 0.60,
                _GOLD_BUCKET: 0.60,
            }
        if any(keyword in text for keyword in ("falls", "fell", "down", "declines", "decrease", "lower")):
            return {
                _RISK_BUCKET: 0.62,
                _DURATION_BUCKET: 0.72,
                _CASH_BUCKET: 0.42,
                _GOLD_BUCKET: 0.44,
            }
    if "real average hourly earnings" in text:
        if any(keyword in text for keyword in ("decrease", "down", "falls")):
            return {
                _RISK_BUCKET: 0.44,
                _DURATION_BUCKET: 0.54,
                _CASH_BUCKET: 0.52,
                _GOLD_BUCKET: 0.50,
            }
        return {
            _RISK_BUCKET: 0.56,
            _DURATION_BUCKET: 0.46,
            _CASH_BUCKET: 0.48,
            _GOLD_BUCKET: 0.50,
        }
    if any(keyword in text for keyword in ("employment situation", "job openings", "unemployment")):
        return {
            _RISK_BUCKET: 0.52,
            _DURATION_BUCKET: 0.46,
            _CASH_BUCKET: 0.48,
            _GOLD_BUCKET: 0.50,
        }
    return None


def _score_treasury_release(title: str) -> dict[str, float] | None:
    text = title.lower()
    if any(keyword in text for keyword in ("borrowing estimates", "quarterly refunding", "treasury borrowing advisory committee", "tbac")):
        return {
            _RISK_BUCKET: 0.45,
            _DURATION_BUCKET: 0.35,
            _CASH_BUCKET: 0.55,
            _GOLD_BUCKET: 0.56,
        }
    if "private credit" in text:
        return {
            _RISK_BUCKET: 0.48,
            _DURATION_BUCKET: 0.48,
            _CASH_BUCKET: 0.52,
            _GOLD_BUCKET: 0.50,
        }
    return None


def _score_sec_form(form: str) -> float | None:
    normalized = form.upper().strip()
    if not normalized:
        return None
    if normalized in {"NT 10-K", "NT 10-Q", "12B-25"}:
        return 0.15
    if normalized.endswith("/A"):
        return 0.35
    if normalized in {"8-K", "6-K"}:
        return 0.40
    if normalized in {"10-K", "10-Q", "20-F", "40-F"}:
        return 0.55
    if normalized.startswith(("S-", "F-")) or normalized.startswith("424B"):
        return 0.30
    if normalized in {"DEF 14A", "DEFA14A", "SC 13D", "SC 13G", "3", "4", "5"}:
        return 0.50
    return 0.50


def _aggregate_bucket_score(
    items: list[OfficialNewsItem], bucket: str, as_of: date
) -> float | None:
    weighted_values = []
    for item in items:
        score = item.impact_scores.get(bucket)
        if score is None:
            continue
        weighted_values.append((score, _recency_weight(item.published_on, as_of)))
    if not weighted_values:
        return None
    total_weight = sum(weight for _, weight in weighted_values)
    return sum(score * weight for score, weight in weighted_values) / total_weight


def _bucket_for_asset(
    symbol: str, asset_type: str | None, benchmark_index: str | None
) -> str | None:
    symbol = symbol.upper()
    asset_type = (asset_type or "").lower()
    benchmark = (benchmark_index or "").upper()
    if asset_type == "equity":
        return _RISK_BUCKET
    if symbol in {"GLD", "IAU", "GOLD"} or benchmark == "GOLD":
        return _GOLD_BUCKET
    if symbol in {"SHY", "BIL", "SGOV"} or benchmark == "UST1_3":
        return _CASH_BUCKET
    if symbol in {"TLT", "IEF"} or benchmark in {"UST20", "UST7_10"}:
        return _DURATION_BUCKET
    if symbol in {"SPY", "QQQ", "IWM", "EFA", "EEM"}:
        return _RISK_BUCKET
    if symbol in {"SPX", "NDX", "RTY", "MXEA", "MXEF"}:
        return _RISK_BUCKET
    if symbol.startswith("UST"):
        if symbol == "UST1_3":
            return _CASH_BUCKET
        return _DURATION_BUCKET
    if asset_type in {"", "etf", "index", "unknown"}:
        return _RISK_BUCKET
    return None


def _company_symbols_from_assets(assets: dict[str, ResearchAsset]) -> tuple[str, ...]:
    return tuple(
        symbol
        for symbol, asset in assets.items()
        if asset.asset_type == "equity"
    )


def _recency_weight(published_on: date, as_of: date) -> float:
    age_days = max(0, (as_of - published_on).days)
    return math.exp(-0.18 * age_days)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    return _clean_text(parser.text())


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _parse_any_date(value: str | None) -> date | None:
    if not value:
        return None
    raw = value.strip()
    date_formats = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%B %d, %Y",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
    )
    for fmt in date_formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).date()
    except ValueError:
        return None
