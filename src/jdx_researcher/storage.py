"""SQLite owns jobs/evidence; graph checkpoints live in a separate database."""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    SCHEMA_VERSION,
    BudgetExceeded,
    ConflictError,
    FinalReport,
    Finding,
    JobCancelled,
    LeaseLost,
    ResearchRequest,
    Source,
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = data_dir / "evidence.sqlite3"
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                    request TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL, deadline REAL,
                    owner TEXT, lease_until REAL, usage TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    job_id TEXT NOT NULL REFERENCES jobs(id), key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(job_id,key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
                    at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    job_id TEXT NOT NULL REFERENCES jobs(id), id TEXT NOT NULL,
                    url TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(job_id,id)
                );
                CREATE TABLE IF NOT EXISTS passages (
                    job_id TEXT NOT NULL, id TEXT NOT NULL, source_id TEXT NOT NULL,
                    text TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(job_id,id),
                    FOREIGN KEY(job_id,source_id) REFERENCES sources(job_id,id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(job_id UNINDEXED, id UNINDEXED, text);
                CREATE TABLE IF NOT EXISTS reports (
                    job_id TEXT NOT NULL REFERENCES jobs(id), version INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(job_id,version)
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, job_id, kind, data):
        db.execute("INSERT INTO events(job_id,at,kind,data) VALUES(?,?,?,?)",
                   (job_id, now(), kind, encode(data)))

    def start(self, request: ResearchRequest) -> dict:
        payload = request.model_dump(mode="json")
        fingerprint = digest(encode(payload))
        with self.db(write=True) as db:
            old = db.execute("SELECT * FROM jobs WHERE idem=?", (request.idempotency_key,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ConflictError("Idempotency key already used with a different request")
                return self._job(old)
            job_id = uuid.uuid4().hex
            usage = dict(queries=0, documents=0, steps=0, llm_calls=0, input_tokens=0, output_tokens=0)
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, request.idempotency_key, fingerprint, encode(payload), "queued",
                        "queued", now(), now(), None, None, None, encode(usage)))
            self._event(db, job_id, "queued", {})
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    @staticmethod
    def _job(row) -> dict:
        data = dict(row)
        data["request"] = json.loads(data["request"])
        data["usage"] = json.loads(data["usage"])
        data["job_id"] = data.pop("id")
        for key in ("idem", "fingerprint", "owner", "lease_until"):
            data.pop(key, None)
        data["schema_version"] = SCHEMA_VERSION
        return data

    def job(self, job_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Unknown job")
            return self._job(row)

    def events(self, job_id: str, limit=30) -> list:
        self.job(job_id)
        with self.db() as db:
            rows = db.execute("SELECT * FROM events WHERE job_id=? ORDER BY seq DESC LIMIT ?",
                              (job_id, limit)).fetchall()
            return [dict(row) | {"data": json.loads(row["data"])} for row in reversed(rows)]

    def claim(self, owner: str, lease_seconds: float) -> str | None:
        stamp = time.time()
        with self.db(write=True) as db:
            row = db.execute("""
                SELECT * FROM jobs WHERE status='queued'
                OR (status='running' AND lease_until < ?) ORDER BY created LIMIT 1
            """, (stamp,)).fetchone()
            if not row:
                return None
            budget = json.loads(row["request"])["budget"]
            deadline = row["deadline"] or stamp + budget["max_runtime_seconds"]
            db.execute("UPDATE jobs SET status='running',owner=?,lease_until=?,deadline=?,updated=? WHERE id=?",
                       (owner, stamp + lease_seconds, deadline, now(), row["id"]))
            self._event(db, row["id"], "claimed", {"resumed": row["status"] == "running"})
            return row["id"]

    def heartbeat(self, job_id: str, owner: str, seconds: float) -> bool:
        with self.db(write=True) as db:
            return db.execute("""
                UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'
            """, (time.time() + seconds, job_id, owner)).rowcount == 1

    @staticmethod
    def _guard(db, job_id: str, owner: str, deadline=True):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError("Unknown job")
        if row["status"] == "cancelled":
            raise JobCancelled()
        if row["status"] != "running" or row["owner"] != owner or row["lease_until"] < time.time():
            raise LeaseLost()
        if deadline and row["deadline"] < time.time():
            raise BudgetExceeded("Runtime limit reached")
        return row

    def guard(self, job_id: str, owner: str):
        with self.db() as db:
            self._guard(db, job_id, owner)

    def event(self, job_id, owner, kind, data):
        with self.db(write=True) as db:
            self._guard(db, job_id, owner)
            self._event(db, job_id, kind, data)

    def stage(self, job_id, owner, stage):
        with self.db(write=True) as db:
            self._guard(db, job_id, owner)
            db.execute("UPDATE jobs SET stage=?,updated=? WHERE id=?", (stage, now(), job_id))
            self._event(db, job_id, "stage", {"stage": stage})

    def reserve(self, job_id, owner, kind: str):
        columns = {"queries": "max_queries", "documents": "max_documents", "steps": "max_agent_steps"}
        if kind not in columns:
            raise ValueError("Unknown budget counter")
        with self.db(write=True) as db:
            row = self._guard(db, job_id, owner)
            usage, request = json.loads(row["usage"]), json.loads(row["request"])
            if usage[kind] >= request["budget"][columns[kind]]:
                raise BudgetExceeded(f"{kind} limit reached")
            usage[kind] += 1
            db.execute("UPDATE jobs SET usage=?,updated=? WHERE id=?", (encode(usage), now(), job_id))
            self._event(db, job_id, "reserved", {"counter": kind, "value": usage[kind]})

    def record_usage(self, job_id, owner, usage):
        with self.db(write=True) as db:
            row = self._guard(db, job_id, owner)
            counters = json.loads(row["usage"])
            counters["llm_calls"] += 1
            counters["input_tokens"] += max(0, int(usage.get("prompt_tokens", 0)))
            counters["output_tokens"] += max(0, int(usage.get("completion_tokens", 0)))
            db.execute("UPDATE jobs SET usage=? WHERE id=?", (encode(counters), job_id))

    def artifact(self, job_id, key):
        with self.db() as db:
            row = db.execute("SELECT value FROM artifacts WHERE job_id=? AND key=?", (job_id, key)).fetchone()
            return json.loads(row["value"]) if row else None

    def put(self, job_id, owner, key, value):
        with self.db(write=True) as db:
            self._guard(db, job_id, owner)
            db.execute("INSERT INTO artifacts VALUES(?,?,?) ON CONFLICT(job_id,key) DO UPDATE SET value=excluded.value",
                       (job_id, key, encode(value)))

    def artifacts(self, job_id, prefix):
        with self.db() as db:
            rows = db.execute("SELECT key,value FROM artifacts WHERE job_id=? AND key LIKE ? ORDER BY key",
                              (job_id, prefix + "%")).fetchall()
            return [json.loads(row["value"]) for row in rows]

    def write_object(self, content: bytes) -> str:
        key = digest(content)
        directory = self.data_dir / "objects" / key[:2]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / key
        if not path.exists():
            temp = directory / (key + "." + uuid.uuid4().hex + ".tmp")
            try:
                temp.write_bytes(content)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return key

    def object(self, key: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid object hash")
        value = (self.data_dir / "objects" / key[:2] / key).read_bytes()
        if digest(value) != key:
            raise ValueError("Object integrity check failed")
        return value

    def save_source(self, job_id, owner, source: Source, action_key: str):
        value = source.model_dump(mode="json")
        with self.db(write=True) as db:
            self._guard(db, job_id, owner)
            exists = db.execute("SELECT 1 FROM sources WHERE job_id=? AND id=?", (job_id, source.source_id)).fetchone()
            if not exists:
                db.execute("INSERT INTO sources VALUES(?,?,?,?)",
                           (job_id, source.source_id, source.original_url, encode(value)))
                for p in source.passages:
                    db.execute("INSERT INTO passages VALUES(?,?,?,?,?)",
                               (job_id, p.passage_id, source.source_id, p.text, encode(p.model_dump())))
                    db.execute("INSERT INTO passage_fts VALUES(?,?,?)", (job_id, p.passage_id, p.text))
            db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?)",
                       (job_id, action_key, encode({"source_id": source.source_id})))
            self._event(db, job_id, "source_saved", {"source_id": source.source_id, "passages": len(source.passages)})

    def sources(self, job_id):
        with self.db() as db:
            return [json.loads(r["value"]) for r in db.execute(
                "SELECT value FROM sources WHERE job_id=? ORDER BY id", (job_id,)).fetchall()]

    def passage(self, job_id, passage_id):
        with self.db() as db:
            row = db.execute("SELECT value FROM passages WHERE job_id=? AND id=?", (job_id, passage_id)).fetchone()
            if not row:
                raise ValueError("Passage does not belong to this job")
            return json.loads(row["value"])

    def passages(self, job_id):
        with self.db() as db:
            return [json.loads(r["value"]) for r in db.execute(
                "SELECT value FROM passages WHERE job_id=? ORDER BY source_id,id", (job_id,)).fetchall()]

    def retrieve(self, job_id, query, limit=12):
        words = re.findall(r"\w{2,}", query, re.UNICODE)[:40]
        if not words:
            return []
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        with self.db() as db:
            rows = db.execute("""
                SELECT p.value FROM passage_fts f JOIN passages p ON p.job_id=f.job_id AND p.id=f.id
                WHERE passage_fts MATCH ? AND f.job_id=? ORDER BY bm25(passage_fts) LIMIT ?
            """, (expression, job_id, limit)).fetchall()
            return [json.loads(r["value"]) for r in rows]

    def save_finding(self, job_id, owner, finding: Finding):
        questions = self.artifact(job_id, "plan")["subquestions"]
        if finding.subquestion >= len(questions):
            raise ValueError("Unknown subquestion")
        for link in finding.evidence:
            self.passage(job_id, link.passage_id)
        value = finding.model_dump(mode="json")
        key = "finding:" + digest(encode(value))[:24]
        self.put(job_id, owner, key, value)
        return key

    def finish(self, job_id, owner, status, issue=None):
        if status not in ("awaiting_review", "partial", "failed"):
            raise ValueError("Invalid worker final state")
        with self.db(write=True) as db:
            self._guard(db, job_id, owner, deadline=False)
            if issue:
                self._event(db, job_id, "issue", {"message": issue})
                db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?)",
                           (job_id, "failure", encode({"message": issue})))
            db.execute("UPDATE jobs SET status=?,stage='delivered',owner=NULL,lease_until=NULL,updated=? WHERE id=?",
                       (status, now(), job_id))
            self._event(db, job_id, status, {})

    def release(self, job_id, owner):
        with self.db(write=True) as db:
            db.execute("UPDATE jobs SET lease_until=0 WHERE id=? AND owner=? AND status='running'", (job_id, owner))

    def cancel(self, job_id):
        self.job(job_id)
        with self.db(write=True) as db:
            changed = db.execute("""
                UPDATE jobs SET status='cancelled',stage='cancelled',owner=NULL,lease_until=NULL,updated=?
                WHERE id=? AND status IN ('queued','running')
            """, (now(), job_id)).rowcount
            if changed:
                self._event(db, job_id, "cancelled", {})
        return self.job(job_id)

    def issues(self, job_id):
        issues = list(self.artifact(job_id, "issues") or [])
        failure = self.artifact(job_id, "failure")
        if failure:
            issues.append(failure["message"])
        for finding in self.artifacts(job_id, "finding:"):
            if any(e["relation"] == "contradicts" for e in finding["evidence"]):
                issues.append("Open contradictory evidence: " + finding["text"])
        return sorted(set(issues))

    def finalize(self, job_id, report: FinalReport):
        # Citation checks and final-state update share one transaction with cancellation.
        payload = report.model_dump(mode="json")
        fingerprint = digest(encode(payload))
        with self.db(write=True) as db:
            job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError("Unknown job")
            if job["status"] not in ("awaiting_review", "partial", "completed"):
                raise ConflictError("Only delivered jobs can be finalized")
            passages = {r["id"]: json.loads(r["value"]) for r in db.execute(
                "SELECT id,value FROM passages WHERE job_id=?", (job_id,))}
            refs = {ref for claim in report.claims for ref in claim.citations}
            markers = set(re.findall(r"\[\[(p_[a-zA-Z0-9_]+)\]\]", report.markdown))
            if not (refs | markers) <= passages.keys():
                raise ValueError("Report contains a reference outside this job")
            if not refs <= markers:
                raise ValueError("Each cited passage must also appear as [[passage_id]] in markdown")
            for ref in refs | markers:
                p = passages[ref]
                source = json.loads(db.execute("SELECT value FROM sources WHERE job_id=? AND id=?",
                                               (job_id, p["source_id"])).fetchone()["value"])
                text = self.object(source["text_sha256"]).decode()
                if text[p["start"]:p["end"]] != p["text"]:
                    raise ValueError("Stored passage integrity check failed")
            previous = db.execute("SELECT version,fingerprint,value FROM reports WHERE job_id=? ORDER BY version DESC LIMIT 1",
                                  (job_id,)).fetchone()
            if previous and previous["fingerprint"] == fingerprint:
                return json.loads(previous["value"])
            # Open issues remain server-owned even when a caller omits them.
            stored_issues = db.execute("SELECT value FROM artifacts WHERE job_id=? AND key IN ('issues','failure')",
                                      (job_id,)).fetchall()
            open_issues = []
            for row in stored_issues:
                item = json.loads(row["value"])
                open_issues.extend(item if isinstance(item, list) else [item["message"]])
            for row in db.execute("SELECT value FROM artifacts WHERE job_id=? AND key LIKE 'finding:%'", (job_id,)):
                finding = json.loads(row["value"])
                if any(e["relation"] == "contradicts" for e in finding["evidence"]):
                    open_issues.append("Open contradictory evidence: " + finding["text"])
            version = previous["version"] + 1 if previous else 1
            review = "needs_attention" if open_issues else (
                "reviewed" if all(c.verified for c in report.claims) else "pending")
            result = payload | {"schema_version": SCHEMA_VERSION, "version": version,
                                "review_status": review, "open_issues": sorted(set(open_issues)),
                                "created_at": now()}
            db.execute("INSERT INTO reports VALUES(?,?,?,?)", (job_id, version, fingerprint, encode(result)))
            db.execute("UPDATE jobs SET status='completed',stage='finalized',updated=? WHERE id=?", (now(), job_id))
            self._event(db, job_id, "finalized", {"version": version, "review_status": review})
            return result

    def report(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT value FROM reports WHERE job_id=? ORDER BY version DESC LIMIT 1", (job_id,)).fetchone()
            return json.loads(row["value"]) if row else None

    def result(self, job_id, cursor=0, limit=8):
        if cursor < 0 or not 1 <= limit <= 20:
            raise ValueError("Invalid pagination")
        job = self.job(job_id)
        passages = self.passages(job_id)
        selected, chars = [], 0
        for p in passages[cursor:cursor + limit]:
            if selected and chars + len(p["text"]) > 24000:
                break
            selected.append(p)
            chars += len(p["text"])
        source_meta = [{k: v for k, v in source.items() if k != "passages"} for source in self.sources(job_id)]
        next_cursor = cursor + len(selected)
        return {
            "schema_version": SCHEMA_VERSION, "job": job,
            "plan": self.artifact(job_id, "plan"), "findings": self.artifacts(job_id, "finding:"),
            "sources": source_meta, "passages": selected, "total_passages": len(passages),
            "next_cursor": next_cursor if next_cursor < len(passages) else None,
            "issues": self.issues(job_id), "report": self.report(job_id),
            "manifest": self.artifact(job_id, "manifest"),
            "notice": "Relations are proposed; structural citation validation does not establish factual truth.",
        }
