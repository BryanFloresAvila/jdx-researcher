"""stdio clients connect to the independent daemon; disconnecting does not stop jobs."""
import httpx


class HttpGateway:
    def __init__(self, settings):
        host = "[" + settings.bind + "]" if ":" in settings.bind else settings.bind
        self.client = httpx.AsyncClient(
            base_url=f"http://{host}:{settings.port}",
            headers={"Authorization": "Bearer " + settings.token()},
            timeout=30, trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        if response.is_error:
            raise ValueError(f"Daemon returned {response.status_code}: {response.text[:400]}")
        return response.json()

    async def research_start(self, request):
        return await self.request("POST", "/v1/research", json=request.model_dump(mode="json"))

    async def research_status(self, job_id):
        return await self.request("GET", f"/v1/research/{job_id}")

    async def research_result(self, job_id, cursor=0, limit=8):
        return await self.request("GET", f"/v1/research/{job_id}/result", params={"cursor": cursor, "limit": limit})

    async def research_source(self, job_id, source_id, passage_id=None):
        params = {"passage_id": passage_id} if passage_id else {}
        return await self.request("GET", f"/v1/research/{job_id}/sources/{source_id}", params=params)

    async def research_finalize(self, job_id, report):
        return await self.request("POST", f"/v1/research/{job_id}/finalize", json=report.model_dump(mode="json"))

    async def research_cancel(self, job_id):
        return await self.request("POST", f"/v1/research/{job_id}/cancel")
