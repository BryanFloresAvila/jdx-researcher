"""Provider clients are trusted configuration, isolated from source downloads.

Code, model and paper providers merge several views (relevance, popularity, recent activity and
emerging work) so frontier sources are not crowded out by the top relevance hits. Stars, likes
and downloads are passed through as raw context in the snippet, never as a quality score.
"""
import itertools
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import httpx

from .models import Query, SearchResult

WINDOW_DAYS = {"week": 7, "month": 30, "year": 365}
DEFAULT_WINDOW_DAYS = 180
HF_MODELS = "https://huggingface.co/api/models"
# Words that appear in research questions but never narrow a model repo name.
GENERIC = {"the", "and", "for", "with", "that", "best", "new", "recent", "latest", "open", "source",
           "model", "models", "small", "large", "local", "llm", "llms", "language", "which", "what"}


def hf_terms(text):
    keywords = [w for w in re.findall(r"[\w.\-]+", text) if len(w) > 2 and w.lower() not in GENERIC]
    return list(dict.fromkeys([text] + keywords[:3]))


def interleave(views, limit):
    """Round-robin across views so each one contributes before any view's tail does."""
    seen, merged = set(), []
    for batch in itertools.zip_longest(*views):
        for item in batch:
            if item is not None and item.url not in seen:
                seen.add(item.url)
                merged.append(item)
    return merged[:limit]


class SearchProviders:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds, follow_redirects=False,
            trust_env=False, transport=transport,
            headers={"User-Agent": "jdx-researcher/0.1", "Accept": "application/json"})

    async def close(self):
        await self.client.aclose()

    async def search(self, query: Query, limit=8):
        if query.provider == "github":
            return interleave(await self._views(self._github_views(query)), limit)
        if query.provider == "huggingface":
            term = await self._hf_term(query.query)
            return interleave(await self._views(self._huggingface_views(query, term)), limit)
        if query.provider == "arxiv":
            return interleave(await self._views([self._arxiv_view(query, False),
                                                 self._arxiv_view(query, True)]), limit)
        params = {"q": query.query, "language": query.language, "format": "json", "safesearch": 0}
        if query.recency in WINDOW_DAYS:
            params["time_range"] = query.recency
        response = await self.client.get(self.settings.searxng_url.rstrip("/") + "/search", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload.get("results"), list):
            raise ValueError("SearXNG JSON results are unavailable; enable formats: [html, json]")
        return [SearchResult(url=item["url"], title=item.get("title") or item["url"],
                             snippet=item.get("content") or "", provider="web",
                             published_at=item.get("publishedDate"))
                for item in payload["results"][:limit] if item.get("url")]

    @staticmethod
    async def _views(views):
        """One failing view (e.g. a rate limit) must not discard the others."""
        results, error = [], None
        for view in views:
            try:
                results.append(await view())
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                error = exc
                results.append([])
        if error is not None and not any(results):
            raise error
        return results

    @staticmethod
    def _since(query):
        days = WINDOW_DAYS.get(query.recency, DEFAULT_WINDOW_DAYS)
        return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()

    def _github_views(self, query):
        headers = {"Accept": "application/vnd.github+json"}
        if self.settings.github_token:
            headers["Authorization"] = "Bearer " + self.settings.github_token.get_secret_value()
        since = self._since(query)

        def view(name, q, sort=None):
            async def run():
                params = {"q": q, "per_page": 10} | ({"sort": sort, "order": "desc"} if sort else {})
                response = await self.client.get("https://api.github.com/search/repositories",
                                                 params=params, headers=headers)
                response.raise_for_status()
                return [SearchResult(
                    url=item["html_url"], title=item["full_name"], provider="github",
                    published_at=item.get("created_at"),
                    snippet=(f"{item.get('description') or ''} [view: {name}; stars "
                             f"{item.get('stargazers_count', 0)}; created {(item.get('created_at') or '')[:10]}; "
                             f"pushed {(item.get('pushed_at') or '')[:10]}]"))
                    for item in response.json().get("items", [])]
            return run
        return [view("relevance", query.query),
                view("popular", query.query, "stars"),
                view("active", f"{query.query} pushed:>{since} stars:>=20", "updated"),
                view("emerging", f"{query.query} created:>{since} stars:10..1000", "stars")]

    async def _hf_term(self, text):
        """Model search matches repo names, so a full sentence finds nothing; fall back to keywords."""
        candidates = hf_terms(text)
        if len(candidates) == 1:
            return text
        for candidate in candidates:
            try:
                response = await self.client.get(HF_MODELS, params={"search": candidate, "limit": 1})
                response.raise_for_status()
            except httpx.HTTPError:
                break
            if response.json():
                return candidate
        return text

    def _huggingface_views(self, query, term):
        def models(name, sort, limit=10, active_only=False):
            async def run():
                response = await self.client.get(HF_MODELS, params={
                    "search": term, "sort": sort, "direction": -1, "limit": limit})
                response.raise_for_status()
                return [SearchResult(
                    url=f"https://huggingface.co/{m['id']}", title=m["id"], provider="huggingface",
                    published_at=m.get("createdAt"),
                    snippet=(f"{m.get('pipeline_tag') or 'model'} [view: {name}; likes {m.get('likes', 0)}; "
                             f"downloads {m.get('downloads', 0)}; created {(m.get('createdAt') or '')[:10]}]"))
                    for m in response.json()
                    # Brand-new uploads with no activity at all are mostly empty placeholders.
                    if not active_only or m.get("likes") or m.get("downloads")]
            return run

        async def papers():
            response = await self.client.get("https://huggingface.co/api/papers/search", params={"q": query.query})
            response.raise_for_status()
            return [SearchResult(
                url=f"https://huggingface.co/papers/{item['paper']['id']}", provider="huggingface",
                title=item.get("title") or item["paper"].get("title") or item["paper"]["id"],
                snippet=(item.get("summary") or "")[:400], published_at=item.get("publishedAt"))
                for item in response.json()[:10]]
        return [models("trending", "trendingScore"), models("new", "createdAt", 50, active_only=True),
                models("popular", "downloads"), papers]

    def _arxiv_view(self, query, newest):
        async def run():
            params = {"search_query": "all:" + query.query, "max_results": 10}
            if newest:
                params |= {"sortBy": "submittedDate", "sortOrder": "descending"}
            response = await self.client.get("https://export.arxiv.org/api/query", params=params)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            return [SearchResult(
                url=entry.findtext("a:id", "", ns).replace("http://", "https://", 1),
                title=" ".join(entry.findtext("a:title", "", ns).split()),
                snippet=entry.findtext("a:summary", "", ns).strip(), provider="arxiv",
                published_at=entry.findtext("a:published", None, ns))
                for entry in root.findall("a:entry", ns)]
        return run
