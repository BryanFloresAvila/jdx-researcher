import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .mcp_server import build_mcp
from .models import ConflictError, FinalReport, ResearchRequest
from .service import Service


class LocalAuth:
    """Pure ASGI middleware, including MCP; no streaming middleware buffering."""
    def __init__(self, app, token, port):
        self.app, self.token, self.port = app, token, port

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        try:
            parsed = urlsplit("http://" + host)
            host_ok = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        except ValueError:
            host_ok = False
        origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}", f"http://[::1]:{self.port}"}
        if not host_ok or (headers.get("origin") and headers["origin"] not in origins):
            return await JSONResponse({"error": "Untrusted host or origin"}, status_code=403)(scope, receive, send)
        if scope["path"] != "/health":
            expected = "Bearer " + self.token
            if not secrets.compare_digest(headers.get("authorization", ""), expected):
                return await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, *, service: Service | None = None):
    settings = settings or Settings()
    service = service or Service(settings)
    server = build_mcp(service)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            # Mounted sub-applications do not run their own lifespan.
            async with server.session_manager.run():
                yield
        finally:
            await service.stop()

    app = FastAPI(title="JDX Researcher", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.mcp = server
    app.add_middleware(LocalAuth, token=settings.token(), port=settings.port)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return JSONResponse({"error": "Unknown job or source"}, status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/health")
    async def health():
        return {"status": "ok", "schema_version": "1.0"}

    @app.post("/v1/research", status_code=202)
    async def start(request: ResearchRequest):
        return await service.research_start(request)

    @app.get("/v1/research/{job_id}")
    async def status(job_id: str):
        return await service.research_status(job_id)

    @app.get("/v1/research/{job_id}/result")
    async def result(job_id: str, cursor: int = Query(0, ge=0), limit: int = Query(8, ge=1, le=20)):
        return await service.research_result(job_id, cursor, limit)

    @app.get("/v1/research/{job_id}/sources/{source_id}")
    async def source(job_id: str, source_id: str, passage_id: str | None = None):
        return await service.research_source(job_id, source_id, passage_id)

    @app.post("/v1/research/{job_id}/finalize")
    async def finalize(job_id: str, report: FinalReport):
        return await service.research_finalize(job_id, report)

    @app.post("/v1/research/{job_id}/cancel")
    async def cancel(job_id: str):
        return await service.research_cancel(job_id)

    # /mcp is handled by the SDK below all explicit HTTP routes.
    app.mount("/", mcp_app)
    return app
