import httpx
import pytest
from pydantic import SecretStr

from jdx_researcher.models import Query
from jdx_researcher.search import SearchProviders


def repo(name, stars=5):
    return {"html_url": f"https://github.com/{name}", "full_name": name, "description": "d",
            "stargazers_count": stars, "created_at": "2026-09-01T00:00:00Z", "pushed_at": "2026-10-25T00:00:00Z"}


def github_handler(seen, fail=()):
    def handle(request):
        seen.append(request)
        q, sort = request.url.params["q"], request.url.params.get("sort")
        view = ("emerging" if "created:>" in q else "active" if "pushed:>" in q
                else "popular" if sort == "stars" else "relevance")
        if view in fail:
            return httpx.Response(403)
        items = {"relevance": [repo("a/a"), repo("b/b")], "popular": [repo("b/b"), repo("c/c", 900)],
                 "active": [repo("d/d")], "emerging": [repo("e/e", 40)]}[view]
        return httpx.Response(200, json={"items": items})
    return handle


async def test_github_merges_relevance_popular_active_and_emerging_views(settings):
    settings.github_token = SecretStr("secret-token")
    seen = []
    search = SearchProviders(settings, transport=httpx.MockTransport(github_handler(seen)))
    try:
        results = await search.search(Query(query="deep research agent", provider="github", recency="month"))
    finally:
        await search.close()
    assert [r.title for r in results] == ["a/a", "b/b", "d/d", "e/e", "c/c"]
    assert "view: emerging; stars 40" in results[3].snippet
    assert len(seen) == 4 and all(r.headers["Authorization"] == "Bearer secret-token" for r in seen)
    emerging = next(r for r in seen if "created:>" in r.url.params["q"])
    assert "stars:10..1000" in emerging.url.params["q"]


async def test_one_failing_view_keeps_the_others_but_all_failing_raises(settings):
    search = SearchProviders(settings, transport=httpx.MockTransport(github_handler([], fail={"popular"})))
    try:
        results = await search.search(Query(query="agents", provider="github"))
        assert {r.title for r in results} == {"a/a", "b/b", "d/d", "e/e"}
    finally:
        await search.close()
    every = {"relevance", "popular", "active", "emerging"}
    search = SearchProviders(settings, transport=httpx.MockTransport(github_handler([], fail=every)))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await search.search(Query(query="agents", provider="github"))
    finally:
        await search.close()


async def test_huggingface_mixes_trending_new_popular_and_papers(settings):
    def model(name, likes=0, downloads=0):
        return {"id": name, "likes": likes, "downloads": downloads, "pipeline_tag": "text-generation",
                "createdAt": "2026-10-20T00:00:00.000Z"}
    def handle(request):
        if request.url.path == "/api/papers/search":
            return httpx.Response(200, json=[{"paper": {"id": "2610.00001"}, "title": "Agent paper",
                                              "summary": "Summary", "publishedAt": "2026-10-25T00:00:00.000Z"}])
        return httpx.Response(200, json={
            "trendingScore": [model("org/hot", 10, 100)],
            "createdAt": [model("org/empty-upload"), model("org/fresh", likes=3)],
            "downloads": [model("org/hot", 10, 100), model("org/popular", 1, 10**6)],
        }[request.url.params["sort"]])
    search = SearchProviders(settings, transport=httpx.MockTransport(handle))
    try:
        results = await search.search(Query(query="agent", provider="huggingface"))
    finally:
        await search.close()
    assert [r.url for r in results] == ["https://huggingface.co/org/hot", "https://huggingface.co/org/fresh",
                                        "https://huggingface.co/papers/2610.00001",
                                        "https://huggingface.co/org/popular"]
    assert "view: new; likes 3" in results[1].snippet


async def test_huggingface_sentence_falls_back_to_a_keyword_that_matches_repo_names(settings):
    searched = []
    def handle(request):
        if request.url.path == "/api/papers/search":
            assert request.url.params["q"] == "open models for agent tool calling"
            return httpx.Response(200, json=[])
        term = request.url.params["search"]
        searched.append(term)
        hit = [{"id": "org/agent-4b", "likes": 2, "downloads": 5, "createdAt": "2026-10-20T00:00:00.000Z"}]
        return httpx.Response(200, json=hit if term == "agent" else [])
    search = SearchProviders(settings, transport=httpx.MockTransport(handle))
    try:
        results = await search.search(Query(query="open models for agent tool calling", provider="huggingface"))
    finally:
        await search.close()
    assert searched[:2] == ["open models for agent tool calling", "agent"]
    assert set(searched[2:]) == {"agent"}
    assert [r.title for r in results] == ["org/agent-4b"]


async def test_web_recency_maps_to_searxng_time_range(settings):
    seen = []
    def handle(request):
        seen.append(request.url.params)
        return httpx.Response(200, json={"results": [{"url": "https://example.org/x", "title": "X"}]})
    search = SearchProviders(settings, transport=httpx.MockTransport(handle))
    try:
        await search.search(Query(query="new models", recency="month"))
        await search.search(Query(query="new models"))
    finally:
        await search.close()
    assert seen[0]["time_range"] == "month" and "time_range" not in seen[1]


async def test_arxiv_adds_newest_first_view(settings):
    seen = []
    def handle(request):
        seen.append(request.url.params)
        index = len(seen)
        return httpx.Response(200, text=(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            f"<id>http://arxiv.org/abs/2610.0000{index}</id><title>T{index}</title>"
            "<summary>S</summary><published>2026-10-26T00:00:00Z</published></entry></feed>"))
    search = SearchProviders(settings, transport=httpx.MockTransport(handle))
    try:
        results = await search.search(Query(query="deep research", provider="arxiv"))
    finally:
        await search.close()
    assert "sortBy" not in seen[0] and seen[1]["sortBy"] == "submittedDate"
    assert [r.url for r in results] == ["https://arxiv.org/abs/2610.00001", "https://arxiv.org/abs/2610.00002"]
