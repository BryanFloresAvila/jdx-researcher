import json

import pytest

from jdx_researcher.config import Settings
from jdx_researcher.evaluation import evaluate
from jdx_researcher.maintenance import backup, restore
from jdx_researcher.service import DaemonLock
from jdx_researcher.storage import Store
from conftest import save_text


def test_backup_restore_preserves_versioned_evidence(prepared, tmp_path):
    store, job_id, owner = prepared
    original = save_text(store, job_id, owner)
    archive = tmp_path / "backup.tar.gz"
    store.release(job_id, owner)
    backup(store.data_dir, archive)
    destination = tmp_path / "restored"
    restore(archive, destination)
    restored = Store(destination)
    assert restored.sources(job_id)[0]["body_sha256"] == original.body_sha256
    assert restored.object(original.text_sha256)
    assert not (destination / "api-token").exists()


def test_backup_refuses_running_daemon(prepared, tmp_path):
    store, _, _ = prepared
    lock = DaemonLock(store.data_dir)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            backup(store.data_dir, tmp_path / "backup.tar.gz")
    finally:
        lock.release()


def test_token_is_persisted_and_redacted(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.token() == Settings(data_dir=tmp_path).token()
    assert "api_token" not in settings.public_manifest()
    assert (tmp_path / "api-token").stat().st_mode & 0o077 == 0


async def test_evaluation_metrics_and_leakage(tmp_path):
    corpus = tmp_path / "pairs.jsonl"
    rows = [
        {"question_id": "q1", "query": "APNs throughput", "passage_id": "p1",
         "text": "APNs throughput measurements", "relevance": 2,
         "split": "test", "label_origin": "human_reviewed"},
        {"question_id": "q1", "query": "APNs throughput", "passage_id": "p2",
         "text": "Banana recipes", "relevance": 0,
         "split": "test", "label_origin": "human_reviewed"},
    ]
    corpus.write_text("\n".join(json.dumps(row) for row in rows))
    result = await evaluate(corpus, top_k=1)
    assert result["per_question"][0]["recall"] == 1
    assert result["per_question"][0]["ndcg"] == 1
    rows[1]["split"] = "train"
    corpus.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="leakage"):
        await evaluate(corpus)
