import asyncio
import json

import httpx
import pytest

from jdx_researcher.llm import LocalModel
from jdx_researcher.models import Query, ResearchRequest, SearchResult
from jdx_researcher.service import Service
from jdx_researcher.storage import Store
from conftest import wait_delivered


class FakeSearch:
    def __init__(self):
        self.calls = 0

    async def search(self, query, limit=6):
        self.calls += 1
        return [SearchResult(url=f"https://example.org/{i}", title=f"Study {i}", provider="web")
                for i in (1, 2)]

    async def close(self):
        pass


class FakeFetcher:
    def __init__(self, block_second=False):
        self.urls = []
        self.block_second = block_second
        self.blocked = asyncio.Event()

    async def fetch(self, url):
        self.urls.append(url)
        if self.block_second and url.endswith("/2"):
            self.blocked.set()
            await asyncio.Event().wait()
        text = ("Throughput is 42 requests/s. Limitations: hardware conditions are missing. " * 4)
        return url, "text/plain", text.encode()

    async def close(self):
        pass


@pytest.mark.parametrize("objective", [
    "Comparar rendimiento de servidores locales",
    "Revisar limitaciones de inferencia local",
    "Compare public benchmark evidence",
    "Investigate source contradictions",
    "Evaluate versioned technical documentation",
])
async def test_five_synthetic_end_to_end_jobs(settings, objective):
    service = Service(settings)
    await service.start()
    try:
        job_id = (await service.research_start(
            ResearchRequest(objective=objective, idempotency_key=objective)))["job_id"]
        job = await wait_delivered(service, job_id)
        assert job["status"] == "partial"
        result = await service.research_result(job_id)
        assert len(result["sources"]) == 3
        assert result["findings"] and result["issues"]
        assert all(f["origin"] == "demo" for f in result["findings"])
    finally:
        await service.stop()


async def test_resume_mid_download_reuses_committed_evidence(settings):
    settings.mode = "retrieval"
    settings.download_concurrency = 1
    first_search, first_fetch = FakeSearch(), FakeFetcher(block_second=True)
    first = Service(settings, search=first_search, fetcher=first_fetch)
    await first.start()
    request = ResearchRequest(objective="Compare throughput limitations", idempotency_key="resume")
    job_id = (await first.research_start(request))["job_id"]
    async with asyncio.timeout(10):
        await first_fetch.blocked.wait()
    original_sources = first.store.sources(job_id)
    assert len(original_sources) == 1
    await first.stop()
    second_search, second_fetch = FakeSearch(), FakeFetcher()
    second = Service(settings, search=second_search, fetcher=second_fetch)
    await second.start()
    try:
        job = await wait_delivered(second, job_id)
        assert job["status"] == "partial"
        assert second_search.calls == 0
        assert second_fetch.urls == ["https://example.org/2"]
        assert len(second.store.sources(job_id)) == 2
        assert original_sources[0]["source_id"] in {s["source_id"] for s in second.store.sources(job_id)}
        assert second.store.job(job_id)["usage"]["queries"] == 1
        assert second.store.job(job_id)["usage"]["documents"] == 3
        assert second.store.start(request)["job_id"] == job_id
    finally:
        await second.stop()


async def test_cancel_pending_download_prevents_late_result(settings):
    settings.mode = "retrieval"
    class LateFetcher(FakeFetcher):
        async def fetch(self, url):
            self.blocked.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return url, "text/plain", b"A late response that must not be committed."
    fetcher = LateFetcher()
    service = Service(settings, search=FakeSearch(), fetcher=fetcher)
    await service.start()
    try:
        job_id = (await service.research_start(
            ResearchRequest(objective="Cancellation test query", idempotency_key="cancel")))["job_id"]
        async with asyncio.timeout(5):
            await fetcher.blocked.wait()
        await service.research_cancel(job_id)
        async with asyncio.timeout(5):
            while service.active_job == job_id:
                await asyncio.sleep(0.02)
        assert service.store.job(job_id)["status"] == "cancelled"
        assert service.store.sources(job_id) == []
    finally:
        await service.stop()


async def test_second_daemon_refused(settings):
    first, second = Service(settings), Service(settings)
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            await second.start()
    finally:
        await first.stop()
        await second.stop()


def completion(name, arguments, call_id="call_1", reason="tool_calls"):
    return {"choices": [{"finish_reason": reason, "message": {
        "role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6}}


async def test_actual_local_agent_contract(settings):
    settings.mode = "local"
    seen = []
    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if payload["tools"][0]["function"]["name"] == "submit_plan":
            return httpx.Response(200, json=completion("submit_plan", {
                "subquestions": ["What is measured throughput?"],
                "queries": [{"query": "throughput limitations", "provider": "web", "language": "en", "subquestion": 0}]}))
        user = json.loads(payload["messages"][1]["content"])
        pid = user["passages"][0]["passage_id"]
        if not any(m["role"] == "tool" for m in payload["messages"]):
            return httpx.Response(200, json=completion("record_finding", {
                "text": "The source reports throughput under unspecified hardware conditions.",
                "subquestion": 0, "conditions": {"hardware": "unspecified"},
                "evidence": [{"passage_id": pid, "relation": "supports"}]}))
        return httpx.Response(200, json=completion("finish_research", {"gaps": []}, call_id="finish"))
    model = LocalModel(settings, transport=httpx.MockTransport(respond))
    service = Service(settings, model=model, search=FakeSearch(), fetcher=FakeFetcher())
    await service.start()
    try:
        job_id = service.store.start(ResearchRequest(objective="What is measured throughput?", idempotency_key="agent"))["job_id"]
        job = await wait_delivered(service, job_id)
        assert job["status"] == "awaiting_review"
        assert service.store.artifacts(job_id, "finding:")
        assert job["usage"]["llm_calls"] == 3
        assert all("tool_calls" not in payload or payload["stream"] is False for payload in seen)
    finally:
        await service.stop()


async def test_invalid_model_plan_repaired_once(settings):
    settings.mode = "local"
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=completion("submit_plan", {"invalid_field": True}))
    model = LocalModel(settings, transport=httpx.MockTransport(respond))
    service = Service(settings, model=model, search=FakeSearch(), fetcher=FakeFetcher())
    await service.start()
    try:
        job_id = service.store.start(ResearchRequest(objective="Tool argument repair test", idempotency_key="invalid"))["job_id"]
        assert (await wait_delivered(service, job_id))["status"] == "failed"
        assert len(calls) == 2
    finally:
        await service.stop()


@pytest.mark.parametrize("payload", [
    {"choices": [{"message": {"content": "<tool_call>not structured</tool_call>"}}]},
    completion("submit_plan", {}, reason="length"),
    {"choices": []},
])
async def test_runtime_protocol_errors_visible(settings, payload):
    model = LocalModel(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    try:
        with pytest.raises(ValueError):
            await model.complete([{"role": "user", "content": "Plan"}], [])
    finally:
        await model.close()


async def test_search_errors_cached_and_counted(prepared, settings):
    from jdx_researcher.ranking import LexicalRanker
    from jdx_researcher.search import SearchProviders
    from jdx_researcher.workflow import Workflow
    store, job_id, owner = prepared
    search = SearchProviders(settings, transport=httpx.MockTransport(lambda _: httpx.Response(429)))
    workflow = Workflow(store, settings, owner, None, search, None, LexicalRanker())
    try:
        query = Query(query="test throughput")
        assert (await workflow.do_search(job_id, query))["error"] == "HTTPStatusError"
        await workflow.do_search(job_id, query)
        assert store.job(job_id)["usage"]["queries"] == 1
    finally:
        await search.close()
