from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from html.parser import HTMLParser
import math
import re
from urllib import error, request

from .models import OfficialNewsContext, OfficialNewsItem
from .tls import build_ssl_context, tls_help_message

_LISTING_PAGE_PATTERNS = (
    "https://www.bls.gov/home.htm",
    "https://home.treasury.gov/news/press-releases",
)


@dataclass(frozen=True)
class LLMNewsSymbolScore:
    symbol: str
    score: float


@dataclass(frozen=True)
class LLMNewsItemAnalysis:
    item_index: int
    summary: str
    confidence: float
    risk_on_score: float
    duration_score: float
    cash_score: float
    gold_score: float
    symbol_scores: tuple[LLMNewsSymbolScore, ...] = ()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self._parts)


def analyze_official_news_context_with_llm(
    context: OfficialNewsContext,
    *,
    api_key: str,
    model: str,
    base_url: str,
    max_items: int,
    max_chars: int,
    user_agent: str,
    require_success: bool,
) -> OfficialNewsContext:
    if not api_key:
        message = "LLM news analysis enabled but OPENAI_API_KEY is missing."
        if require_success:
            raise RuntimeError(message)
        return replace(
            context,
            source_status={**context.source_status, "llm": f"disabled: {message}"},
        )

    candidate_items = list(context.items[: max(0, max_items)])
    if not candidate_items:
        return replace(
            context,
            source_status={**context.source_status, "llm": "skipped (no news items)"},
        )

    try:
        analyses = _request_llm_news_analysis(
            items=candidate_items,
            api_key=api_key,
            model=model,
            base_url=base_url,
            max_chars=max_chars,
            user_agent=user_agent,
        )
    except RuntimeError as exc:
        if require_success:
            raise
        return replace(
            context,
            source_status={**context.source_status, "llm": f"error: {exc}; fallback to rules"},
        )

    return _apply_llm_news_analyses(context, analyses=analyses, model=model)


def _apply_llm_news_analyses(
    context: OfficialNewsContext,
    *,
    analyses: list[LLMNewsItemAnalysis],
    model: str,
) -> OfficialNewsContext:
    analyses_by_index = {analysis.item_index: analysis for analysis in analyses}
    updated_items: list[OfficialNewsItem] = []
    llm_company_values: dict[str, list[tuple[float, float]]] = {}

    for index, item in enumerate(context.items):
        analysis = analyses_by_index.get(index)
        if analysis is None:
            updated_items.append(item)
            continue

        updated_scores = dict(item.impact_scores)
        if item.source in {"fed", "bls", "treasury"}:
            updated_scores = {
                "risk_on": _blend_score(item.impact_scores.get("risk_on"), analysis.risk_on_score, analysis.confidence),
                "duration": _blend_score(item.impact_scores.get("duration"), analysis.duration_score, analysis.confidence),
                "cash": _blend_score(item.impact_scores.get("cash"), analysis.cash_score, analysis.confidence),
                "gold": _blend_score(item.impact_scores.get("gold"), analysis.gold_score, analysis.confidence),
            }
        elif item.source == "sec":
            updated_scores["risk_on"] = _blend_score(
                item.impact_scores.get("risk_on"),
                analysis.risk_on_score,
                analysis.confidence,
            )

        for symbol_score in analysis.symbol_scores:
            weight = _recency_weight(item.published_on, context.as_of) * max(
                0.1, analysis.confidence
            )
            llm_company_values.setdefault(symbol_score.symbol, []).append(
                (_clamp01(symbol_score.score), weight)
            )

        updated_items.append(
            replace(
                item,
                impact_scores=updated_scores,
                analysis_summary=analysis.summary.strip() or None,
                analysis_confidence=_clamp01(analysis.confidence),
            )
        )

    company_scores = dict(context.company_scores)
    for symbol, weighted_values in llm_company_values.items():
        total_weight = sum(weight for _, weight in weighted_values)
        if total_weight <= 0:
            continue
        llm_score = sum(value * weight for value, weight in weighted_values) / total_weight
        if symbol in company_scores:
            llm_score = 0.75 * llm_score + 0.25 * company_scores[symbol]
        company_scores[symbol] = _clamp01(llm_score)

    return replace(
        context,
        risk_on_score=_aggregate_bucket_score(updated_items, "risk_on", context.as_of),
        duration_score=_aggregate_bucket_score(updated_items, "duration", context.as_of),
        cash_score=_aggregate_bucket_score(updated_items, "cash", context.as_of),
        gold_score=_aggregate_bucket_score(updated_items, "gold", context.as_of),
        company_scores=company_scores,
        items=tuple(updated_items),
        source_status={
            **context.source_status,
            "llm": f"ok ({len(analyses)} items via {model})",
        },
    )


def _request_llm_news_analysis(
    *,
    items: list[OfficialNewsItem],
    api_key: str,
    model: str,
    base_url: str,
    max_chars: int,
    user_agent: str,
) -> list[LLMNewsItemAnalysis]:
    documents = []
    for index, item in enumerate(items):
        documents.append(
            {
                "item_index": index,
                "source": item.source,
                "published_on": item.published_on.isoformat(),
                "title": item.title,
                "url": item.url,
                "symbols": list(item.symbols),
                "summary": item.summary or "",
                "document_text": _build_document_text(
                    item,
                    user_agent=user_agent,
                    max_chars=max_chars,
                ),
            }
        )

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a cautious investment research assistant. "
                    "Analyze only the supplied official-source documents. "
                    "Return conservative structured scores for 1-20 trading day impact. "
                    "Use 0.5 when the effect is neutral or unclear."
                ),
            },
            {
                "role": "user",
                "content": (
                    "For each document, provide a short summary, a confidence score from 0 to 1, "
                    "macro bucket scores from 0 to 1 for risk_on, duration, cash, and gold, "
                    "and any company-specific symbol scores from 0 to 1 when the document materially affects a named issuer.\n\n"
                    f"Documents:\n{json.dumps(documents, indent=2)}"
                ),
            },
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "official_news_analysis",
                "strict": True,
                "schema": _analysis_schema(),
            },
        },
    }
    response_payload = _post_openai_json(
        url=f"{base_url.rstrip('/')}/chat/completions",
        api_key=api_key,
        payload=payload,
    )
    try:
        message = response_payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenAI response did not include a usable chat completion.") from exc

    refusal = message.get("refusal")
    if refusal:
        raise RuntimeError(f"OpenAI refused the request: {refusal}")

    content = _message_text(message.get("content"))
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI returned invalid JSON: {exc}") from exc

    analyses: list[LLMNewsItemAnalysis] = []
    for item in parsed.get("items", []):
        analyses.append(
            LLMNewsItemAnalysis(
                item_index=int(item["item_index"]),
                summary=str(item["summary"]).strip(),
                confidence=_clamp01(float(item["confidence"])),
                risk_on_score=_clamp01(float(item["risk_on_score"])),
                duration_score=_clamp01(float(item["duration_score"])),
                cash_score=_clamp01(float(item["cash_score"])),
                gold_score=_clamp01(float(item["gold_score"])),
                symbol_scores=tuple(
                    LLMNewsSymbolScore(
                        symbol=str(value["symbol"]).upper(),
                        score=_clamp01(float(value["score"])),
                    )
                    for value in item.get("symbol_scores", [])
                ),
            )
        )
    return analyses


def _build_document_text(
    item: OfficialNewsItem,
    *,
    user_agent: str,
    max_chars: int,
) -> str:
    sections = [f"Title: {item.title}"]
    if item.summary:
        sections.append(f"Summary: {item.summary}")
    if item.symbols:
        sections.append(f"Symbols: {', '.join(item.symbols)}")
    if item.url and not _is_listing_page(item.url):
        try:
            raw_text = _fetch_url_text(item.url, user_agent=user_agent)
        except RuntimeError:
            raw_text = ""
        cleaned = _clean_text(raw_text)
        if cleaned:
            sections.append(f"Document text: {cleaned[:max_chars]}")
    return "\n".join(sections)[:max_chars]


def _post_openai_json(*, url: str, api_key: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    req = request.Request(
        url,
        headers=headers,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with request.urlopen(req, timeout=45, context=build_ssl_context()) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="replace"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenAI API request failed ({exc.code}) at {url}: {detail}"
        ) from exc
    except error.URLError as exc:
        raise RuntimeError(
            "Unable to establish a trusted HTTPS connection to OpenAI. "
            f"{tls_help_message()} Original error: {exc}"
        ) from exc


def _fetch_url_text(url: str, *, user_agent: str) -> str:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=20, context=build_ssl_context()) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (error.HTTPError, error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"unable to fetch {url}: {exc}") from exc


def _analysis_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_index": {"type": "integer"},
                        "summary": {"type": "string"},
                        "confidence": {"type": "number"},
                        "risk_on_score": {"type": "number"},
                        "duration_score": {"type": "number"},
                        "cash_score": {"type": "number"},
                        "gold_score": {"type": "number"},
                        "symbol_scores": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "symbol": {"type": "string"},
                                    "score": {"type": "number"},
                                },
                                "required": ["symbol", "score"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": [
                        "item_index",
                        "summary",
                        "confidence",
                        "risk_on_score",
                        "duration_score",
                        "cash_score",
                        "gold_score",
                        "symbol_scores",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def _aggregate_bucket_score(
    items: list[OfficialNewsItem], bucket: str, as_of: date
) -> float | None:
    weighted_values = []
    for item in items:
        score = item.impact_scores.get(bucket)
        if score is None:
            continue
        weighted_values.append((_clamp01(score), _recency_weight(item.published_on, as_of)))
    if not weighted_values:
        return None
    total_weight = sum(weight for _, weight in weighted_values)
    return sum(score * weight for score, weight in weighted_values) / total_weight


def _blend_score(
    base_score: float | None,
    llm_score: float,
    confidence: float,
) -> float:
    llm_score = _clamp01(llm_score)
    confidence = _clamp01(confidence)
    if base_score is None:
        return llm_score
    return (confidence * llm_score) + ((1.0 - confidence) * _clamp01(base_score))


def _recency_weight(published_on: date, as_of: date) -> float:
    age_days = max(0, (as_of - published_on).days)
    return math.exp(-0.18 * age_days)


def _is_listing_page(url: str) -> bool:
    return any(pattern in url for pattern in _LISTING_PAGE_PATTERNS)


def _clean_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    return re.sub(r"\s+", " ", parser.text()).strip()


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
