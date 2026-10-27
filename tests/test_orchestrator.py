from jdx_researcher.orchestrator import (
    GAP_PREFIX,
    OrchestrationRequest,
    orchestrate,
    render_markdown,
)


class FakeGateway:
    """Each subquestion resolves once it has been attempted `attempts_needed` times."""

    def __init__(self, attempts_needed=None, gap_issues=True):
        self.attempts_needed = attempts_needed or {}
        self.gap_issues = gap_issues
        self.attempts, self.jobs, self.status_calls = {}, {}, {}

    async def research_start(self, request):
        job_id = f"job{len(self.jobs)}"
        findings, issues, passages = [], [], []
        for index, question in enumerate(request.subquestions):
            self.attempts[question] = self.attempts.get(question, 0) + 1
            pid = f"p_{job_id}_{index}"
            passages.append({"passage_id": pid, "source_id": "s_shared", "text": f"Evidence about {question}"})
            if self.attempts[question] >= self.attempts_needed.get(question, 1):
                findings.append({"text": f"Answer to {question}", "subquestion": index,
                                 "evidence": [{"passage_id": pid, "relation": "supports"}]})
            elif self.gap_issues:
                issues.append(GAP_PREFIX + question)
        self.jobs[job_id] = {"request": request, "findings": findings, "issues": issues, "passages": passages}
        return {"job_id": job_id}

    async def research_status(self, job_id):
        self.status_calls[job_id] = self.status_calls.get(job_id, 0) + 1
        status = "running" if self.status_calls[job_id] == 1 else "partial"
        return {"job": {"status": status, "usage": {"steps": 1}}}

    async def research_result(self, job_id, cursor=0, limit=8):
        job = self.jobs[job_id]
        page = job["passages"][cursor:cursor + 1]
        next_cursor = cursor + 1 if cursor + 1 < len(job["passages"]) else None
        source = {"source_id": "s_shared", "title": "Shared", "final_url": "https://example.org/a",
                  "captured_at": "2026-10-26T00:00:00+00:00", "published_at": None, "family_id": "exact_same"}
        return {"issues": job["issues"], "findings": job["findings"], "sources": [source],
                "passages": page, "next_cursor": next_cursor}


def make_request(angles, **extra):
    return OrchestrationRequest(objective="Compare local runtimes", idempotency_key="lh",
                                angles=[{"subquestions": a} for a in angles], **extra)


async def test_follow_up_round_closes_structural_gap():
    gateway = FakeGateway({"B": 2})
    result = await orchestrate(gateway, make_request([["A", "B"], ["C"]]), poll_seconds=0)
    assert result["stop_reason"] == "coverage"
    assert [j["subquestions"] for j in result["jobs"]] == [["A", "B"], ["C"], ["B"]]
    assert [j["round"] for j in result["jobs"]] == [0, 0, 1]
    assert [j["request"].idempotency_key for j in gateway.jobs.values()] == ["lh:r0:0", "lh:r0:1", "lh:r1:0"]
    assert {f["question"] for f in result["findings"]} == {"A", "B", "C"}
    assert all(link["text"].startswith("Evidence about") for f in result["findings"] for link in f["evidence"])
    assert result["open_gaps"] == [] and len(result["sources"]) == 1
    assert "- none" in render_markdown(result)


async def test_failed_job_without_gap_issues_still_gets_follow_up():
    gateway = FakeGateway({"A": 2}, gap_issues=False)
    result = await orchestrate(gateway, make_request([["A"]]), poll_seconds=0)
    assert result["stop_reason"] == "coverage"
    assert [j["subquestions"] for j in result["jobs"]] == [["A"], ["A"]]


async def test_meta_budget_stops_before_spawning_more_jobs():
    result = await orchestrate(FakeGateway(), make_request([["A"], ["B"]], max_jobs=1), poll_seconds=0)
    assert result["stop_reason"] == "max_jobs"
    assert len(result["jobs"]) == 1 and result["open_gaps"] == ["B"]


async def test_round_without_new_findings_stops_as_no_progress():
    gateway = FakeGateway({"B": 99})
    result = await orchestrate(gateway, make_request([["A", "B"]], max_rounds=3), poll_seconds=0)
    assert result["stop_reason"] == "no_progress"
    assert len(result["jobs"]) == 2 and result["open_gaps"] == ["B"]


async def test_round_limit_keeps_remaining_gaps_open():
    result = await orchestrate(FakeGateway({"B": 2}), make_request([["A", "B"]], max_rounds=0), poll_seconds=0)
    assert result["stop_reason"] == "max_rounds"
    assert result["open_gaps"] == ["B"] and len(result["jobs"]) == 1
