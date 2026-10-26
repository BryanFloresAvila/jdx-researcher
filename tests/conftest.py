import asyncio

import pytest

from jdx_researcher.config import Settings
from jdx_researcher.models import ResearchRequest
from jdx_researcher.readers import Extracted, make_source
from jdx_researcher.storage import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", mode="demo", api_token="test-token",
                    poll_seconds=0.02, lease_seconds=3)


@pytest.fixture
def prepared(settings):
    store = Store(settings.data_dir)
    request = ResearchRequest(objective="Compare model throughput and limitations", idempotency_key="unit")
    job_id = store.start(request)["job_id"]
    assert store.claim("owner", 60) == job_id
    store.put(job_id, "owner", "plan", {"subquestions": [request.objective], "queries": []})
    return store, job_id, "owner"


def save_text(store, job_id, owner, text="Model throughput is 42 requests/s. Limitations apply.", url="https://example.org/study"):
    extracted = Extracted("Study", text, None, [(None, 0, len(text))], "fixture/1", [])
    source = make_source(url, url, text.encode(), "text/plain", extracted, store)
    store.save_source(job_id, owner, source, "read:" + source.source_id)
    return source


async def wait_delivered(service, job_id):
    async with asyncio.timeout(15):
        while service.store.job(job_id)["status"] in ("queued", "running"):
            await asyncio.sleep(0.02)
    return service.store.job(job_id)
