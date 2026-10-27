"""Client-side long-horizon loop over the daemon.

Each bounded daemon job acts as a sub-agent. After every round a deterministic meta-reasoner
(MIRA-style outer loop) reads the persistent record and issues follow-up work orders only for
structural gaps; a meta-budget (Budget Guard style) decides when to stop. The daemon keeps its
single scheduler: this module only issues more research_start calls through a gateway.
"""
import asyncio
from datetime import date
from typing import Any, Literal

from pydantic import Field

from .models import Budget, Contract, ResearchRequest

GAP_PREFIX = "No proposed support for subquestion: "
DELIVERED = {"awaiting_review", "completed", "partial", "cancelled", "failed"}


class Angle(Contract):
    subquestions: list[str] = Field(min_length=1, max_length=4)
    seed_urls: list[str] = Field(default_factory=list, max_length=30)


class OrchestrationRequest(Contract):
    objective: str = Field(min_length=5, max_length=6000)
    idempotency_key: str = Field(min_length=1, max_length=150)
    angles: list[Angle] = Field(min_length=1, max_length=8)
    constraints: dict[str, Any] = Field(default_factory=dict)
    languages: list[Literal["es", "en"]] = Field(default_factory=lambda: ["es", "en"], min_length=1, max_length=2)
    output_language: Literal["es", "en"] = "es"
    as_of: date | None = None
    budget: Budget = Field(default_factory=Budget)
    max_rounds: int = Field(1, ge=0, le=3)
    max_jobs: int = Field(6, ge=1, le=12)


def supported(findings, index):
    return any(f["subquestion"] == index and any(e["relation"] == "supports" for e in f["evidence"])
               for f in findings)


async def collect(gateway, job_id, poll_seconds):
    while True:
        status = (await gateway.research_status(job_id))["job"]
        if status["status"] in DELIVERED:
            break
        await asyncio.sleep(poll_seconds)
    package = await gateway.research_result(job_id, 0, 20)
    passages, cursor = list(package["passages"]), package["next_cursor"]
    while cursor is not None:
        page = await gateway.research_result(job_id, cursor, 20)
        passages.extend(page["passages"])
        cursor = page["next_cursor"]
    return {"status": status["status"], "usage": status["usage"], "issues": package["issues"],
            "findings": package["findings"], "sources": package["sources"], "passages": passages}


async def orchestrate(gateway, request: OrchestrationRequest, poll_seconds=5.0):
    orders = [angle.model_dump() for angle in request.angles]
    jobs, unfollowed, stop_reason = [], [], "max_rounds"
    for round_index in range(request.max_rounds + 1):
        room = request.max_jobs - len(jobs)
        unfollowed += [q for order in orders[room:] for q in order["subquestions"]]
        records = []
        for index, order in enumerate(orders[:room]):
            job = await gateway.research_start(ResearchRequest(
                objective=request.objective, constraints=request.constraints,
                languages=request.languages, output_language=request.output_language,
                as_of=request.as_of, budget=request.budget,
                subquestions=order["subquestions"], seed_urls=order["seed_urls"],
                idempotency_key=f"{request.idempotency_key}:r{round_index}:{index}"))
            records.append({"round": round_index, "job_id": job["job_id"],
                            "subquestions": order["subquestions"]})
        # Jobs queue on the single daemon scheduler, so collecting in order loses no time.
        for record in records:
            record |= await collect(gateway, record["job_id"], poll_seconds)
        jobs += records
        # Derived from findings, not issue text, so failed or cancelled jobs count as gaps too.
        gaps = list(dict.fromkeys(question for record in records
                                  for index, question in enumerate(record["subquestions"])
                                  if not supported(record["findings"], index)))
        if not gaps:
            stop_reason = "max_jobs" if unfollowed else "coverage"
            break
        if round_index and not any(record["findings"] for record in records):
            stop_reason, unfollowed = "no_progress", unfollowed + gaps
            break
        if round_index == request.max_rounds or len(jobs) >= request.max_jobs:
            stop_reason = "max_rounds" if round_index == request.max_rounds else "max_jobs"
            unfollowed += gaps
            break
        orders = [{"subquestions": gaps[i:i + 4], "seed_urls": []} for i in range(0, len(gaps), 4)]
    return synthesize(request, jobs, unfollowed, stop_reason)


def synthesize(request, jobs, unfollowed, stop_reason):
    findings, sources, seen = [], [], set()
    for job in jobs:
        passages = {p["passage_id"]: p for p in job.pop("passages")}
        for finding in job.pop("findings"):
            index = finding["subquestion"]
            findings.append(finding | {
                "job_id": job["job_id"], "round": job["round"],
                "question": job["subquestions"][index] if index < len(job["subquestions"]) else None,
                "evidence": [link | {"text": passages[link["passage_id"]]["text"]}
                             if link["passage_id"] in passages else link for link in finding["evidence"]]})
        for source in job.pop("sources"):
            # Separate jobs capture the same page with different bytes, so families alone miss it.
            if not {source["family_id"], source["final_url"]} & seen:
                seen |= {source["family_id"], source["final_url"]}
                sources.append({"job_id": job["job_id"], "source_id": source["source_id"],
                                "title": source["title"], "final_url": source["final_url"],
                                "captured_at": source["captured_at"], "published_at": source["published_at"]})
    return {
        "schema_version": "1.0", "objective": request.objective, "stop_reason": stop_reason,
        "jobs": jobs, "findings": findings, "sources": sources,
        "open_gaps": sorted(set(unfollowed)),
        "open_issues": sorted({issue for job in jobs for issue in job["issues"]
                               if not issue.startswith(GAP_PREFIX)}),
        "notice": "Findings are proposed by the local worker and cite passages per job; "
                  "review them semantically before relying on them."}


def render_markdown(result):
    lines = [f"# {result['objective']}", "", f"Stop reason: `{result['stop_reason']}` · "
             f"{len(result['jobs'])} jobs · {len(result['findings'])} proposed findings · "
             f"{len(result['sources'])} distinct sources", "", "## Proposed findings", ""]
    for finding in result["findings"]:
        refs = " ".join(f"[[{finding['job_id']}/{link['passage_id']}]] ({link['relation']})"
                        for link in finding["evidence"])
        lines += [f"- **{finding['question']}** (round {finding['round']}): {finding['text']}", f"  {refs}"]
    lines += ["", "## Open gaps", ""] + ([f"- {gap}" for gap in result["open_gaps"]] or ["- none"])
    lines += ["", "## Open issues", ""] + ([f"- {issue}" for issue in result["open_issues"]] or ["- none"])
    lines += ["", "## Jobs", ""] + [f"- round {job['round']} · `{job['job_id']}` · {job['status']} · "
                                    + "; ".join(job["subquestions"]) for job in result["jobs"]]
    lines += ["", "## Sources", ""] + [f"- {s['title']} — {s['final_url']} (captured {s['captured_at']})"
                                       for s in result["sources"]]
    return "\n".join(lines + ["", result["notice"], ""])
