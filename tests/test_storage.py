import time

import pytest
from pydantic import ValidationError

from jdx_researcher.models import (
    Budget, BudgetExceeded, ConflictError, FinalReport, Finding, JobCancelled,
    LeaseLost, ReportClaim, ResearchRequest,
)
from jdx_researcher.storage import Store
from conftest import save_text


def test_idempotency_survives_new_connection(settings):
    request = ResearchRequest(objective="Compare local tools", idempotency_key="same")
    first = Store(settings.data_dir).start(request)
    second = Store(settings.data_dir).start(request)
    assert first["job_id"] == second["job_id"]


def test_idempotency_conflict(settings):
    store = Store(settings.data_dir)
    store.start(ResearchRequest(objective="First objective", idempotency_key="same"))
    with pytest.raises(ConflictError):
        store.start(ResearchRequest(objective="Different objective", idempotency_key="same"))


def test_atomic_budget(prepared):
    store, job_id, owner = prepared
    maximum = store.job(job_id)["request"]["budget"]["max_queries"]
    for _ in range(maximum):
        store.reserve(job_id, owner, "queries")
    with pytest.raises(BudgetExceeded):
        store.reserve(job_id, owner, "queries")
    assert store.job(job_id)["usage"]["queries"] == maximum


def test_lease_recovery_fences_old_owner(prepared):
    store, job_id, old_owner = prepared
    store.release(job_id, old_owner)
    assert store.claim("new-owner", 60) == job_id
    with pytest.raises(LeaseLost):
        store.put(job_id, old_owner, "late", {"value": "stale"})
    store.put(job_id, "new-owner", "current", True)


def test_live_lease_cannot_be_claimed(prepared):
    store, _, _ = prepared
    assert store.claim("other-owner", 60) is None


def test_runtime_limit_preserved_after_recovery(prepared):
    store, job_id, owner = prepared
    deadline = store.job(job_id)["deadline"]
    store.release(job_id, owner)
    store.claim("next-owner", 60)
    assert store.job(job_id)["deadline"] == deadline


def test_runtime_guard(prepared):
    store, job_id, owner = prepared
    with store.db(write=True) as db:
        db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job_id))
    with pytest.raises(BudgetExceeded):
        store.reserve(job_id, owner, "documents")
    store.finish(job_id, owner, "failed", "deadline")


def test_cancel_blocks_late_artifacts(prepared):
    store, job_id, owner = prepared
    store.cancel(job_id)
    with pytest.raises(JobCancelled):
        store.put(job_id, owner, "late", "discard")
    assert store.artifact(job_id, "late") is None
    assert store.cancel(job_id)["status"] == "cancelled"


def test_cancellation_is_isolated(prepared):
    store, job_id, owner = prepared
    other = store.start(ResearchRequest(objective="Second independent question", idempotency_key="second"))
    store.cancel(other["job_id"])
    store.guard(job_id, owner)


def test_exact_versions_and_localizers(prepared):
    store, job_id, owner = prepared
    first = save_text(store, job_id, owner, "Original measurement: 42 requests/s.")
    second = save_text(store, job_id, owner, "Changed measurement: 27 requests/s.")
    assert first.source_id != second.source_id
    assert len(store.sources(job_id)) == 2
    for source in (first, second):
        text = store.object(source.text_sha256).decode()
        for passage in source.passages:
            assert text[passage.start:passage.end] == passage.text
    assert first.published_at is None


def test_mirror_family(prepared):
    store, job_id, owner = prepared
    a = save_text(store, job_id, owner)
    b = save_text(store, job_id, owner, url="https://mirror.example.org/study")
    assert a.source_id != b.source_id
    assert a.family_id == b.family_id


def test_fts_retrieval_and_query_sanitization(prepared):
    store, job_id, owner = prepared
    source = save_text(store, job_id, owner)
    assert store.retrieve(job_id, 'throughput " OR (') [0]["source_id"] == source.source_id
    assert store.retrieve(job_id, "!!!") == []
    assert store.retrieve("another-job", "throughput") == []


def test_finding_rejects_foreign_reference(prepared):
    store, job_id, owner = prepared
    finding = Finding(text="Foreign evidence proposal", subquestion=0,
                      evidence=[{"passage_id": "p_not_owned", "relation": "supports"}])
    with pytest.raises(ValueError):
        store.save_finding(job_id, owner, finding)


def make_report(passage_id):
    return FinalReport(title="Review", markdown="Measured throughput [[%s]]." % passage_id,
                       claims=[ReportClaim(text="Measured throughput", citations=[passage_id])])


def test_finalize_validates_and_is_idempotent(prepared):
    store, job_id, owner = prepared
    source = save_text(store, job_id, owner)
    store.finish(job_id, owner, "awaiting_review")
    report = make_report(source.passages[0].passage_id)
    result = store.finalize(job_id, report)
    assert result["review_status"] == "pending"
    assert store.finalize(job_id, report)["version"] == 1
    assert store.job(job_id)["status"] == "completed"


def test_report_unknown_citation_rejected(prepared):
    store, job_id, owner = prepared
    save_text(store, job_id, owner)
    store.finish(job_id, owner, "awaiting_review")
    with pytest.raises(ValueError):
        store.finalize(job_id, make_report("p_fake"))


def test_report_requires_markers(prepared):
    store, job_id, owner = prepared
    source = save_text(store, job_id, owner)
    store.finish(job_id, owner, "awaiting_review")
    report = make_report(source.passages[0].passage_id)
    report.markdown = "No citation marker."
    with pytest.raises(ValueError):
        store.finalize(job_id, report)


def test_finalize_detects_tampered_objects(prepared):
    store, job_id, owner = prepared
    source = save_text(store, job_id, owner)
    path = store.data_dir / "objects" / source.text_sha256[:2] / source.text_sha256
    path.write_bytes(b"tampered")
    store.finish(job_id, owner, "awaiting_review")
    with pytest.raises(ValueError):
        store.finalize(job_id, make_report(source.passages[0].passage_id))


def test_open_contradictions_cannot_be_erased(prepared):
    store, job_id, owner = prepared
    source = save_text(store, job_id, owner)
    pid = source.passages[0].passage_id
    store.save_finding(job_id, owner, Finding(text="Contradictory reported throughput",
        subquestion=0, evidence=[{"passage_id": pid, "relation": "contradicts"}]))
    store.put(job_id, owner, "issues", ["Missing hardware conditions"])
    store.finish(job_id, owner, "partial")
    report = make_report(pid)
    result = store.finalize(job_id, report)
    assert result["review_status"] == "needs_attention"
    assert "Missing hardware conditions" in result["open_issues"]
    assert any("Contradictory" in x for x in result["open_issues"])


def test_pagination_never_loses_passages(prepared):
    store, job_id, owner = prepared
    save_text(store, job_id, owner, ("Throughput measurement with limitations.\n" * 500))
    seen, cursor = [], 0
    while cursor is not None:
        result = store.result(job_id, cursor, limit=2)
        seen += [p["passage_id"] for p in result["passages"]]
        cursor = result["next_cursor"]
    assert len(seen) == len(set(seen)) == len(store.passages(job_id))


def test_request_and_verified_claim_contracts():
    with pytest.raises(ValidationError):
        Budget(max_documents=0)
    with pytest.raises(ValidationError):
        ReportClaim(text="Unsupported sourced claim")
    with pytest.raises(ValidationError):
        ReportClaim(text="Unreviewed", citations=["p_1"], verified=True)
    with pytest.raises(ValidationError):
        ResearchRequest(objective="Test objective", idempotency_key="x", subquestions=["x"] * 5)


def test_objects_reject_path_traversal(prepared):
    store, _, _ = prepared
    with pytest.raises(ValueError):
        store.object("../api-token")
