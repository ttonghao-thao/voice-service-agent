"""A bounded read-only Tavily Search adapter."""

import re
from html import unescape
from typing import Literal
from urllib.parse import urlsplit

from app.contracts import DomainError, StrictModel
from app.tools.adapters import bounded_json
from pydantic import Field, TypeAdapter

ENDPOINT = "https://api.tavily.com/search"


class WebSearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    freshness: Literal["pd", "pw", "pm", "py"] | None = None


class WebSource(StrictModel):
    title: str
    url: str
    excerpt: str


class WebSearchResult(StrictModel):
    sources: list[WebSource] = Field(max_length=3)


class WebSearchAdapter:
    input_model = WebSearchInput
    output_adapter = TypeAdapter(WebSearchResult)

    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def invoke(self, args, ctx):
        key = self.settings.web_search_api_key.get_secret_value()
        if not key:
            raise DomainError("TOOL_NOT_CONFIGURED", "Public search is unavailable.", 503)
        payload = {"query": args.query, "max_results": 3, "search_depth": "basic",
                   "include_answer": False, "include_raw_content": False}
        if args.freshness:
            payload["time_range"] = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}[args.freshness]
        data = await bounded_json(
            self.client, "POST", ENDPOINT, retry=False, limit=65536, json=payload,
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        )
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise DomainError("TOOL_BAD_RESPONSE", "Public search returned an invalid response.", 502)
        sources = []
        for item in results[:3]:
            if not isinstance(item, dict):
                continue
            title, url, excerpt = (item.get(name) for name in ("title", "url", "content"))
            if not all(isinstance(value, str) and value.strip() for value in (title, url, excerpt)):
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or len(url) > 1500:
                continue
            sources.append(WebSource(title=plain_text(title)[:180], url=url, excerpt=plain_text(excerpt)[:440]))
        return WebSearchResult(sources=sources)


def plain_text(value):
    return " ".join(unescape(re.sub(r"</?[^>]+>", "", value)).split())
