import asyncio
import importlib.metadata
import json
import platform
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn

from .api import create_app
from .config import Settings, load_settings
from .evaluation import evaluate
from .llm import LocalModel, PLAN_TOOL
from .maintenance import backup, restore
from .mcp_server import build_mcp
from .models import FinalReport, Plan, ResearchRequest
from .orchestrator import OrchestrationRequest, orchestrate, render_markdown
from .proxy import HttpGateway

app = typer.Typer(no_args_is_help=True, help="Local research daemon, durable evidence and MCP.")


def emit(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.callback()
def main(ctx: typer.Context, config: Annotated[Path | None, typer.Option("--config", exists=True)] = None):
    ctx.obj = load_settings(config)


@app.command()
def serve(ctx: typer.Context, mode: Annotated[str | None, typer.Option()] = None):
    """Run one independent daemon. Stop with Ctrl-C."""
    settings = ctx.obj
    if mode:
        settings = Settings.model_validate(settings.model_dump() | {"mode": mode})
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, workers=1)


@app.command("mcp-stdio")
def mcp_stdio(ctx: typer.Context):
    """Connect a stdio client to a running daemon without creating another scheduler."""
    async def run():
        gateway = HttpGateway(ctx.obj)
        try:
            await build_mcp(gateway).run_stdio_async()
        finally:
            await gateway.close()
    asyncio.run(run())


async def invoke(settings, method, *args, **kwargs):
    gateway = HttpGateway(settings)
    try:
        return await getattr(gateway, method)(*args, **kwargs)
    finally:
        await gateway.close()


@app.command()
def start(ctx: typer.Context, request: Annotated[Path, typer.Argument(exists=True)]):
    """Create a job from a JSON request file."""
    emit(asyncio.run(invoke(ctx.obj, "research_start", ResearchRequest.model_validate_json(request.read_text()))))


@app.command()
def status(ctx: typer.Context, job_id: str):
    emit(asyncio.run(invoke(ctx.obj, "research_status", job_id)))


@app.command()
def result(ctx: typer.Context, job_id: str, cursor: int = 0):
    emit(asyncio.run(invoke(ctx.obj, "research_result", job_id, cursor)))


@app.command()
def source(ctx: typer.Context, job_id: str, source_id: str, passage_id: str | None = None):
    emit(asyncio.run(invoke(ctx.obj, "research_source", job_id, source_id, passage_id)))


@app.command()
def cancel(ctx: typer.Context, job_id: str):
    emit(asyncio.run(invoke(ctx.obj, "research_cancel", job_id)))


@app.command()
def finalize(ctx: typer.Context, job_id: str, report: Annotated[Path, typer.Argument(exists=True)]):
    parsed_report = FinalReport.model_validate_json(report.read_text())
    emit(asyncio.run(invoke(ctx.obj, "research_finalize", job_id, parsed_report)))


@app.command()
def export(ctx: typer.Context, job_id: str, output: Annotated[Path, typer.Option("--output")]):
    """Export all evidence pages and the latest finalized report."""
    async def collect():
        gateway = HttpGateway(ctx.obj)
        try:
            package = await gateway.research_result(job_id, 0, 20)
            passages = list(package["passages"])
            cursor = package["next_cursor"]
            while cursor is not None:
                page = await gateway.research_result(job_id, cursor, 20)
                passages.extend(page["passages"])
                cursor = page["next_cursor"]
            package["passages"], package["next_cursor"] = passages, None
            return package
        finally:
            await gateway.close()
    package = asyncio.run(collect())
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence.json").write_text(json.dumps(package, ensure_ascii=False, indent=2))
    report = package["report"]
    if report:
        text = report["markdown"]
        sources = {s["source_id"]: s for s in package["sources"]}
        passage_map = {p["passage_id"]: p for p in package["passages"]}
        references = []
        for ref in sorted({ref for claim in report["claims"] for ref in claim["citations"]}):
            passage = passage_map[ref]
            source = sources[passage["source_id"]]
            page = f", page {passage['page']}" if passage["page"] else ""
            references.append(f"- [[{ref}]] {source['title']} — {source['final_url']}{page}; captured {source['captured_at']}")
        text += "\n\n## Conserved references\n\n" + "\n".join(references) + "\n"
        if report["open_issues"]:
            text += "\n## Open issues\n\n" + "\n".join("- " + x for x in report["open_issues"]) + "\n"
        (output / "report.md").write_text(text)
    emit({"output": str(output.resolve()), "passages": len(package["passages"]),
          "report": bool(report)})


@app.command("orchestrate")
def orchestrate_command(ctx: typer.Context, request: Annotated[Path, typer.Argument(exists=True)],
                        output: Annotated[Path, typer.Option("--output")], poll_seconds: float = 5.0):
    """Run bounded jobs as sub-agents, follow up on structural gaps, then synthesize."""
    parsed = OrchestrationRequest.model_validate_json(request.read_text())
    async def run():
        gateway = HttpGateway(ctx.obj)
        try:
            return await orchestrate(gateway, parsed, poll_seconds)
        finally:
            await gateway.close()
    result = asyncio.run(run())
    output.mkdir(parents=True, exist_ok=True)
    (output / "orchestration.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (output / "summary.md").write_text(render_markdown(result))
    emit({"output": str(output.resolve()), "stop_reason": result["stop_reason"], "jobs": len(result["jobs"]),
          "findings": len(result["findings"]), "open_gaps": result["open_gaps"]})


@app.command()
def token(ctx: typer.Context):
    """Print the local bearer token explicitly; keep it out of committed config files."""
    typer.echo(ctx.obj.token())


@app.command()
def doctor(ctx: typer.Context):
    """Check configured services and record the exact installed software environment."""
    async def check():
        settings = ctx.obj
        checks = {}
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            for name, url, params in [
                ("llm", settings.llm_base_url.rstrip("/") + "/models", None),
                ("searxng", settings.searxng_url.rstrip("/") + "/search", {"q": "test", "format": "json"}),
            ]:
                try:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    data = response.json()
                    checks[name] = {"ok": isinstance(data.get("results" if name == "searxng" else "data"), list)}
                except (httpx.HTTPError, ValueError):
                    checks[name] = {"ok": False}
        return checks
    emit({"python": platform.python_version(), "platform": platform.platform(),
          "packages": {name: importlib.metadata.version(name)
                       for name in ("jdx-researcher", "mcp", "langgraph", "httpcore", "pymupdf", "trafilatura")},
          "config": ctx.obj.public_manifest(), "services": asyncio.run(check()),
          "hardware_benchmark": "Not measured; run probe-model and compare 16K/32K on the target Mac."})


@app.command("probe-model")
def probe_model(ctx: typer.Context):
    """Run 20 live protocol probes; this measures tool integration, not research accuracy."""
    async def probe():
        model = LocalModel(ctx.obj)
        outcomes = []
        try:
            for index in range(20):
                prompt = ("Submit a one-query plan about " +
                          ("APNs HTTP/2 connection reuse in Go" if index % 2 else "llama.cpp Metal en macOS") +
                          f". Use a {'github' if index % 3 == 0 else 'web'} query. Case {index}.")
                try:
                    result = await model.complete(
                        [{"role": "user", "content": prompt}], [PLAN_TOOL],
                        {"type": "function", "function": {"name": "submit_plan"}})
                    calls = result["message"]["tool_calls"]
                    if len(calls) != 1 or calls[0]["function"]["name"] != "submit_plan":
                        raise ValueError("Unexpected tool")
                    Plan.model_validate_json(calls[0]["function"]["arguments"])
                    outcomes.append({"case": index, "valid": True, "usage": result["usage"]})
                except (httpx.HTTPError, ValueError):
                    outcomes.append({"case": index, "valid": False})
        finally:
            await model.close()
        passed = sum(row["valid"] for row in outcomes)
        return {"valid": passed, "total": 20, "integration_gate": passed >= 19, "cases": outcomes,
                "notice": "20 schema probes only; multi-turn behavior is covered separately by software tests."}
    emit(asyncio.run(probe()))


@app.command("backup")
def backup_command(ctx: typer.Context, output: Annotated[Path, typer.Option("--output")]):
    """Stop the daemon before taking an offline backup."""
    emit(backup(ctx.obj.data_dir, output))


@app.command("restore")
def restore_command(archive: Annotated[Path, typer.Argument(exists=True)],
                    destination: Annotated[Path, typer.Option("--destination")]):
    emit(restore(archive, destination))


@app.command("evaluate")
def evaluate_command(corpus: Annotated[Path, typer.Argument(exists=True)], selector: str = "lexical",
                     top_k: int = 5, revision: str = "main", device: str = "auto"):
    if selector not in ("lexical", "qwen3") or top_k < 1:
        raise typer.BadParameter("Use lexical/qwen3 and positive top-k")
    emit(asyncio.run(evaluate(corpus, selector, top_k, revision, device)))
