import asyncio
import json
import socket
import sys

import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from jdx_researcher.api import create_app
from jdx_researcher.models import ResearchRequest
from conftest import wait_delivered


@pytest.fixture
async def running_app(settings):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    settings.port = sock.getsockname()[1]
    application = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=settings.port,
                                         log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(10):
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Server did not start")
            await asyncio.sleep(0.02)
    try:
        yield settings, application
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def test_auth_host_origin_and_idempotency(running_app):
    settings, app = running_app
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.port}") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/v1/research/unknown")).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        assert (await client.get("/v1/research/unknown", headers=headers)).status_code == 404
        assert (await client.get("/health", headers={"Host": "attacker.example.org"})).status_code == 403
        assert (await client.post("/v1/research", json={}, headers=headers | {"Origin": "https://evil.example"})).status_code == 403
        request = ResearchRequest(objective="API demonstration test", idempotency_key="api")
        first = await client.post("/v1/research", json=request.model_dump(mode="json"), headers=headers)
        second = await client.post("/v1/research", json=request.model_dump(mode="json"), headers=headers)
        assert first.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        changed = request.model_dump(mode="json") | {"objective": "Another objective"}
        assert (await client.post("/v1/research", json=changed, headers=headers)).status_code == 409
        assert (await client.get("/v1/research/x/result?limit=100", headers=headers)).status_code == 422
        await wait_delivered(app.state.service, first.json()["job_id"])


def tool_payload(result):
    assert not result.isError
    payload = result.structuredContent
    if payload is None:
        payload = json.loads(next(c.text for c in result.content if c.type == "text"))
    return payload


async def exercise_session(session):
    await session.initialize()
    names = {tool.name for tool in (await session.list_tools()).tools}
    assert names == {"research_start", "research_status", "research_result", "research_source",
                     "research_finalize", "research_cancel"}
    request = ResearchRequest(objective="MCP transport demonstration", idempotency_key="mcp")
    created = tool_payload(await session.call_tool("research_start", {"request": request.model_dump(mode="json")}))
    status = tool_payload(await session.call_tool("research_status", {"job_id": created["job_id"]}))
    assert status["job"]["job_id"] == created["job_id"]
    return created["job_id"]


async def test_streamable_http_real_sdk_roundtrip(running_app):
    settings, app = running_app
    async with streamablehttp_client(f"http://127.0.0.1:{settings.port}/mcp",
                                    headers={"Authorization": "Bearer test-token"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            job_id = await exercise_session(session)
    # Closing the client leaves both daemon and job alive.
    await wait_delivered(app.state.service, job_id)
    assert app.state.service.store.sources(job_id)


async def test_stdio_proxy_roundtrip(running_app, tmp_path):
    settings, app = running_app
    config_path = tmp_path / "client.yaml"
    config_path.write_text(f"data_dir: {settings.data_dir}\nport: {settings.port}\napi_token: test-token\n")
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "jdx_researcher", "--config", str(config_path), "mcp-stdio"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            job_id = await exercise_session(session)
    await wait_delivered(app.state.service, job_id)
    assert app.state.service.store.job(job_id)["status"] == "partial"
