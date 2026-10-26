import asyncio
import fcntl
import logging
import time
import uuid
from contextlib import AsyncExitStack, suppress

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .llm import LocalModel
from .models import BudgetExceeded, JobCancelled, LeaseLost
from .network import SafeFetcher
from .ranking import LexicalRanker, Qwen3Ranker
from .search import SearchProviders
from .storage import Store
from .workflow import Workflow

logger = logging.getLogger(__name__)


class DaemonLock:
    """One scheduler/inference owner per data directory on macOS/Linux."""
    def __init__(self, data_dir):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "daemon.lock"
        self.stream = None

    def acquire(self):
        self.stream = self.path.open("a+")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("A daemon or maintenance operation already owns this data directory") from None

    def release(self):
        if self.stream:
            fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


class Service:
    def __init__(self, settings, *, model=None, search=None, fetcher=None, ranker=None):
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.owner = uuid.uuid4().hex
        self.lock = DaemonLock(settings.data_dir)
        self.model = model or LocalModel(settings)
        self.search = search or SearchProviders(settings)
        self.fetcher = fetcher or SafeFetcher(settings)
        self.ranker = ranker or (
            Qwen3Ranker(settings.reranker_revision, settings.reranker_device)
            if settings.ranking == "qwen3" else LexicalRanker())
        self.stack = AsyncExitStack()
        self.scheduler = self.active_task = None
        self.active_job = None
        self.stopping = False

    async def start(self):
        self.lock.acquire()
        try:
            saver = await self.stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(self.settings.data_dir / "checkpoints.sqlite3")))
            await saver.setup()
            workflow = Workflow(self.store, self.settings, self.owner,
                                self.model, self.search, self.fetcher, self.ranker)
            self.graph = workflow.graph(saver)
            self.scheduler = asyncio.create_task(self._schedule())
        except BaseException:
            await self.stack.aclose()
            self.lock.release()
            raise

    async def stop(self):
        self.stopping = True
        if self.scheduler:
            self.scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await self.scheduler
        await self.stack.aclose()
        for adapter in (self.model, self.search, self.fetcher):
            await adapter.close()
        self.lock.release()

    async def _heartbeat(self, job_id, task):
        while True:
            await asyncio.sleep(self.settings.lease_seconds / 3)
            if not self.store.heartbeat(job_id, self.owner, self.settings.lease_seconds):
                task.cancel()
                return

    async def _schedule(self):
        while not self.stopping:
            job_id = self.store.claim(self.owner, self.settings.lease_seconds)
            if not job_id:
                await asyncio.sleep(self.settings.poll_seconds)
                continue
            self.active_job = job_id
            self.active_task = asyncio.create_task(self.execute(job_id))
            heartbeat = asyncio.create_task(self._heartbeat(job_id, self.active_task))
            try:
                await self.active_task
            except asyncio.CancelledError:
                if self.stopping:
                    raise
            finally:
                self.active_task.cancel()
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                self.store.release(job_id, self.owner)
                self.active_job = self.active_task = None

    async def execute(self, job_id):
        try:
            job = self.store.job(job_id)
            remaining = job["deadline"] - time.time()
            if remaining <= 0:
                raise BudgetExceeded("Runtime limit reached")
            config = {"configurable": {"thread_id": job_id}, "recursion_limit": 20}
            async with asyncio.timeout(remaining):
                snapshot = await self.graph.aget_state(config)
                if snapshot.values:
                    if snapshot.next:
                        await self.graph.ainvoke(None, config)
                    else:
                        # A graph could finish before job metadata is committed after an unusual failure.
                        workflow = Workflow(self.store, self.settings, self.owner,
                                            self.model, self.search, self.fetcher, self.ranker)
                        await workflow.deliver({"job_id": job_id})
                else:
                    await self.graph.ainvoke({"job_id": job_id}, config)
        except (JobCancelled, LeaseLost):
            pass
        except (BudgetExceeded, TimeoutError) as exc:
            with suppress(JobCancelled, LeaseLost):
                status = "partial" if self.store.sources(job_id) else "failed"
                self.store.finish(job_id, self.owner, status, str(exc) or "Runtime limit reached")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Research job %s stopped", job_id)
            with suppress(JobCancelled, LeaseLost):
                status = "partial" if self.store.sources(job_id) else "failed"
                self.store.finish(job_id, self.owner, status, "Research stopped: " + type(exc).__name__)

    async def research_start(self, request):
        return self.store.start(request)

    async def research_status(self, job_id):
        job = self.store.job(job_id)
        return {"schema_version": "1.0", "job": job, "events": self.store.events(job_id),
                "issues": self.store.issues(job_id)}

    async def research_result(self, job_id, cursor=0, limit=8):
        return self.store.result(job_id, cursor, limit)

    async def research_source(self, job_id, source_id, passage_id=None):
        self.store.job(job_id)
        source = next((s for s in self.store.sources(job_id) if s["source_id"] == source_id), None)
        if source is None:
            raise KeyError("Source does not belong to this job")
        if passage_id:
            passage = self.store.passage(job_id, passage_id)
            if passage["source_id"] != source_id:
                raise ValueError("Passage belongs to a different source")
            return {"source": {k: v for k, v in source.items() if k != "passages"}, "passage": passage}
        return {"source": {k: v for k, v in source.items() if k != "passages"},
                "passages": source["passages"][:8], "total_passages": len(source["passages"]),
                "notice": "Use research_result pagination for additional passages"}

    async def research_finalize(self, job_id, report):
        return self.store.finalize(job_id, report)

    async def research_cancel(self, job_id):
        result = self.store.cancel(job_id)
        if self.active_job == job_id and self.active_task:
            self.active_task.cancel()
        return result
