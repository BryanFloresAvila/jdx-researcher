"""Provider clients are trusted configuration, isolated from source downloads."""
import xml.etree.ElementTree as ET

import httpx

from .models import Query, SearchResult


class SearchProviders:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds, follow_redirects=False,
            trust_env=False, transport=transport,
            headers={"User-Agent": "jdx-researcher/0.1", "Accept": "application/json"})

    async def close(self):
        await self.client.aclose()

    async def search(self, query: Query, limit=6):
        if query.provider == "github":
            response = await self.client.get(
                "https://api.github.com/search/repositories",
                params={"q": query.query, "per_page": min(limit, 10)},
                headers={"Accept": "application/vnd.github+json"})
            response.raise_for_status()
            return [SearchResult(url=item["html_url"], title=item["full_name"],
                                 snippet=item.get("description") or "", provider="github")
                    for item in response.json().get("items", [])[:limit]]
        if query.provider == "arxiv":
            response = await self.client.get("https://export.arxiv.org/api/query",
                                             params={"search_query": "all:" + query.query,
                                                     "max_results": min(limit, 10)})
            response.raise_for_status()
            root = ET.fromstring(response.content)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            values = []
            for entry in root.findall("a:entry", ns)[:limit]:
                url = entry.findtext("a:id", "", ns)
                values.append(SearchResult(
                    url=url.replace("http://", "https://", 1),
                    title=" ".join(entry.findtext("a:title", "", ns).split()),
                    snippet=entry.findtext("a:summary", "", ns).strip(), provider="arxiv",
                    published_at=entry.findtext("a:published", None, ns)))
            return values
        response = await self.client.get(
            self.settings.searxng_url.rstrip("/") + "/search",
            params={"q": query.query, "language": query.language, "format": "json", "safesearch": 0})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload.get("results"), list):
            raise ValueError("SearXNG JSON results are unavailable; enable formats: [html, json]")
        return [SearchResult(url=item["url"], title=item.get("title") or item["url"],
                             snippet=item.get("content") or "", provider="web",
                             published_at=item.get("publishedDate"))
                for item in payload["results"][:limit] if item.get("url")]
