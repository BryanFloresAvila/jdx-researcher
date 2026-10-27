import asyncio
import itertools
import json
from datetime import UTC, datetime
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .llm import PLAN_TOOL, SYSTEM, TOOL_MODELS, TOOLS, input_char_budget
from .models import BudgetExceeded, Finding, Plan, Query, ReaderError, ResearchRequest
from .network import validate_url
from .ranking import select
from .readers import Extracted, make_source, read
from .storage import digest, encode


class ResearchState(TypedDict):
    job_id: str


REPEATABLE_TOOLS = ("search_sources", "read_source", "inspect_evidence")


def today(request):
    """The worker's knowledge is older than the job, so it is told the reference date."""
    as_of = request["as_of"] if isinstance(request, dict) else request.as_of
    return str(as_of or datetime.now(UTC).date())


def compact_tool_message(message, chars=160):
    """Shrink a tool result to ids plus short excerpts; ids stay valid for record_finding."""
    if message.get("role") != "tool":
        return message
    try:
        value = json.loads(message["content"])
    except ValueError:
        return message
    if not isinstance(value, dict):
        return message
    if isinstance(value.get("passages"), list):
        value["passages"] = [{"passage_id": p["passage_id"], "excerpt": p["text"][:chars]} for p in value["passages"]]
    if isinstance(value.get("results"), list):
        value["results"] = [{"url": r.get("url"), "title": r.get("title")} for r in value["results"]]
    return message | {"content": encode(value)}


class Workflow:
    def __init__(self, store, settings, owner, model, search, fetcher, ranker):
        self.store, self.settings, self.owner = store, settings, owner
        self.model, self.search, self.fetcher, self.ranker = model, search, fetcher, ranker
        self.downloads = asyncio.Semaphore(settings.download_concurrency)

    def graph(self, checkpointer):
        graph = StateGraph(ResearchState)
        graph.add_node("plan", self.plan)
        graph.add_node("retrieve", self.retrieve)
        graph.add_node("analyze", self.analyze)
        graph.add_node("deliver", self.deliver)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "retrieve")
        graph.add_edge("retrieve", "analyze")
        graph.add_edge("analyze", "deliver")
        graph.add_edge("deliver", END)
        return graph.compile(checkpointer=checkpointer)

    def issue(self, job_id, message):
        values = self.store.artifact(job_id, "issues") or []
        if message not in values:
            self.store.put(job_id, self.owner, "issues", values + [message])

    async def call_model(self, job_id, key, messages, tools, choice="auto"):
        cached = self.store.artifact(job_id, key)
        if cached is not None:
            return cached["message"]
        self.store.reserve(job_id, self.owner, "steps")
        result = await self.model.complete(messages, tools, choice)
        self.store.record_usage(job_id, self.owner, result["usage"])
        self.store.put(job_id, self.owner, key, result)
        return result["message"]

    async def plan(self, state):
        job_id = state["job_id"]
        self.store.guard(job_id, self.owner)
        if self.store.artifact(job_id, "plan"):
            return state
        self.store.stage(job_id, self.owner, "planning")
        self.store.put(job_id, self.owner, "manifest", self.settings.public_manifest())
        request = ResearchRequest.model_validate(self.store.job(job_id)["request"])
        questions = request.subquestions or [request.objective]
        if self.settings.mode != "local":
            plan = Plan(subquestions=questions, queries=[
                Query(query=q[:800], language=request.languages[i % len(request.languages)], subquestion=i)
                for i, q in enumerate(questions)])
        else:
            messages = [{"role": "system", "content": SYSTEM + (
                            "\nSubmit only a research plan. Spread queries across providers: huggingface "
                            "for models and papers, github for code and tools, arxiv for papers, web for "
                            "the rest. Set recency when the objective asks for the latest state. Your "
                            "training data is older than `today`: never add past years to a query.")},
                        {"role": "user", "content": encode(request.model_dump(mode="json") | {"today": today(request)})}]
            # One repair of invalid arguments, using a new persisted attempt key.
            for attempt in range(2):
                message = await self.call_model(
                    job_id, f"model:plan:{attempt}", messages, [PLAN_TOOL],
                    {"type": "function", "function": {"name": "submit_plan"}})
                try:
                    calls = message["tool_calls"]
                    if len(calls) != 1 or calls[0]["function"]["name"] != "submit_plan":
                        raise ValueError("Expected exactly one submit_plan call")
                    plan = Plan.model_validate_json(calls[0]["function"]["arguments"])
                    if request.subquestions:
                        caller = request.subquestions
                        if plan.subquestions[:len(caller)] != caller:
                            raise ValueError("Preserve caller-provided subquestions exactly")
                        # Extra model-proposed subquestions are outside the caller's scope; drop
                        # them and their queries rather than failing the whole job.
                        plan = Plan(subquestions=caller,
                                    queries=[q for q in plan.queries if q.subquestion < len(caller)])
                    if any(q.subquestion >= len(plan.subquestions) for q in plan.queries):
                        raise ValueError("Query references an unknown subquestion")
                    break
                except (ValueError, ValidationError):
                    if attempt:
                        raise ValueError("Model plan failed after one argument repair") from None
                    messages += [message] + [
                        {"role": "tool", "tool_call_id": call["id"],
                         "content": "Invalid plan. Preserve supplied subquestions and use valid indices."}
                        for call in message["tool_calls"]]
            else:
                raise ValueError("No model plan")
        self.store.put(job_id, self.owner, "plan", plan.model_dump(mode="json"))
        return state

    async def do_search(self, job_id, query: Query):
        key = "search:" + digest(encode(query.model_dump()))
        cached = self.store.artifact(job_id, key)
        if cached is not None:
            return cached
        self.store.reserve(job_id, self.owner, "queries")
        try:
            results = [item.model_dump(mode="json") for item in await self.search.search(query)]
            value = {"query": query.model_dump(), "results": results}
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            value = {"query": query.model_dump(), "results": [], "error": type(exc).__name__}
            self.issue(job_id, f"Search failed ({query.provider}, {type(exc).__name__})")
        self.store.put(job_id, self.owner, key, value)
        self.store.event(job_id, self.owner, "search", {"provider": query.provider, "results": len(value["results"])})
        return value

    async def do_read(self, job_id, url):
        url = validate_url(url)
        key = "read:" + digest(url)
        cached = self.store.artifact(job_id, key)
        if cached is not None:
            return cached
        async with self.downloads:
            # Recheck cache after waiting for an identical concurrent read.
            cached = self.store.artifact(job_id, key)
            if cached is not None:
                return cached
            self.store.reserve(job_id, self.owner, "documents")
            try:
                source, warnings = await read(self.fetcher, self.store, url, self.settings)
            except (httpx.HTTPError, ReaderError, ValueError, RuntimeError, TimeoutError) as exc:
                self.store.guard(job_id, self.owner)
                value = {"error": type(exc).__name__, "url": url}
                self.store.put(job_id, self.owner, key, value)
                self.issue(job_id, f"Read failed: {url} ({type(exc).__name__})")
                return value
            self.store.save_source(job_id, self.owner, source, key)
            for warning in warnings:
                self.issue(job_id, warning + ": " + url)
            return {"source_id": source.source_id}

    async def retrieve(self, state):
        job_id = state["job_id"]
        self.store.stage(job_id, self.owner, "retrieving")
        if self.settings.mode == "demo":
            if self.store.artifact(job_id, "demo:retrieved"):
                return state
            self.store.reserve(job_id, self.owner, "queries")
            for index, rate in enumerate((42, 27, 31)):
                key = f"read:demo:{index}"
                if self.store.artifact(job_id, key):
                    continue
                self.store.reserve(job_id, self.owner, "documents")
                text = (f"DEMOSTRACIÓN SINTÉTICA / SYNTHETIC DEMO. Simulated throughput {rate} requests/s. "
                        "Rendimiento simulado de un servidor local. Limitaciones: esta cifra es inventada "
                        "para probar el flujo, no mide hardware ni modelos reales. "
                        "Versions and benchmark conditions differ between these synthetic sources.")
                extracted = Extracted(f"Synthetic fixture {index}", text, None,
                                      [(None, 0, len(text))], "demo/1", [])
                source = make_source(
                    f"https://example.org/demo/{index}", f"https://example.org/demo/{index}",
                    text.encode(), "text/plain", extracted, self.store)
                self.store.save_source(job_id, self.owner, source, key)
            self.issue(job_id, "Synthetic demo: evidence does not describe real measurements")
            self.store.put(job_id, self.owner, "demo:retrieved", True)
            return state
        request = self.store.job(job_id)["request"]
        per_query = []
        for query in self.store.artifact(job_id, "plan")["queries"]:
            try:
                data = await self.do_search(job_id, Query.model_validate(query))
                per_query.append([item["url"] for item in data["results"]])
            except BudgetExceeded as exc:
                self.issue(job_id, str(exc))
                break
        # Round-robin so the document budget reaches every query and provider instead of
        # being spent entirely on the first query's results.
        ranked = [url for batch in itertools.zip_longest(*per_query) for url in batch if url]
        urls = list(dict.fromkeys(request["seed_urls"] + ranked))
        # Each read accounts independently; one bad source does not discard other evidence.
        async def bounded_read(url):
            try:
                await self.do_read(job_id, url)
            except BudgetExceeded as exc:
                self.issue(job_id, str(exc))
            except ReaderError:
                self.issue(job_id, "Blocked invalid or private source URL")
        await asyncio.gather(*(bounded_read(url) for url in urls[:request["budget"]["max_documents"]]))
        return state

    def _work_order(self, job_id, plan, request, progress):
        """MIRA-style meta-reasoning step: a deterministic (no extra LLM call) read of the
        persistent record (findings so far) plus a Budget Guard style feasibility check,
        producing one explicit next-work-order directive instead of a generic reminder."""
        findings = self.store.artifacts(job_id, "finding:")
        coverage = []
        for index, question in enumerate(plan["subquestions"]):
            relations = [e["relation"] for f in findings if f["subquestion"] == index for e in f["evidence"]]
            coverage.append({"subquestion": index, "question": question,
                             "supports": relations.count("supports"),
                             "contradicts": relations.count("contradicts"),
                             "insufficient": relations.count("insufficient")})
        usage, budget = self.store.job(job_id)["usage"], request["budget"]
        remaining_budget = {"steps": budget["max_agent_steps"] - usage["steps"],
                            "documents": budget["max_documents"] - usage["documents"],
                            "queries": budget["max_queries"] - usage["queries"],
                            "gap_rounds": budget["max_gap_rounds"] - progress["gap_rounds"]}
        uncovered = [c["question"] for c in coverage if not (c["supports"] or c["contradicts"] or c["insufficient"])]
        if uncovered and (remaining_budget["steps"] <= 2 or remaining_budget["gap_rounds"] <= 0):
            directive = ("Budget is nearly exhausted. For every subquestion with no finding yet, call "
                         "record_finding now with an insufficient relation naming what is missing, "
                         "then call finish_research.")
        elif uncovered:
            directive = ("No finding recorded yet for: " + "; ".join(uncovered) +
                         ". Call record_finding for one of these now, citing passage ids you already have "
                         "(candidate_passage_ids or the initial passages); use relation insufficient if they "
                         "do not answer it. Only call inspect_evidence with a query you have not used yet.")
        else:
            directive = "Every subquestion already has at least one finding. Call finish_research now."
        return {"coverage": coverage, "remaining_budget": remaining_budget, "next_work_order": directive,
                "candidate_passage_ids": progress.get("candidates", [])[-12:],
                "already_inspected_queries": list(dict.fromkeys(progress.get("inspected", [])))}

    def _fit_context(self, initial, groups, reminder):
        """ContextBudget: compact older tool results first, then the newest, and only then drop
        whole turns, instead of overflowing the worker context and aborting the job."""
        groups = [list(group) for group in groups]
        def fits():
            messages = initial + [message for group in groups for message in group] + reminder
            return len(json.dumps(messages, ensure_ascii=False)) <= input_char_budget(self.settings)
        for index in range(len(groups)):
            if fits():
                break
            groups[index] = [compact_tool_message(message) for message in groups[index]]
        while groups and not fits():
            groups.pop(0)
        return initial + [message for group in groups for message in group] + reminder

    async def analyze(self, state):
        job_id = state["job_id"]
        if self.store.artifact(job_id, "analysis:done"):
            return state
        self.store.stage(job_id, self.owner, "analyzing")
        plan = self.store.artifact(job_id, "plan")
        if self.settings.mode == "demo":
            passages = self.store.passages(job_id)
            for index, question in enumerate(plan["subquestions"]):
                finding = Finding(
                    text="Synthetic conflicting throughput measurements; no real benchmark conclusion.",
                    subquestion=index, origin="demo",
                    evidence=[{"passage_id": p["passage_id"],
                               "relation": "supports" if i == 0 else "contradicts"}
                              for i, p in enumerate(passages[:2])])
                self.store.save_finding(job_id, self.owner, finding)
            self.store.put(job_id, self.owner, "analysis:done", True)
            return state
        if self.settings.mode == "retrieval":
            self.issue(job_id, "Retrieval-only mode: semantic findings require cloud review")
            self.store.put(job_id, self.owner, "analysis:done", True)
            return state
        passages, decisions = await select(self.store, job_id, plan["subquestions"], self.ranker)
        self.store.put(job_id, self.owner, "ranking", decisions)
        progress = self.store.artifact(job_id, "analysis:progress") or {
            "turn": 0, "groups": [], "invalid": 0, "gap_rounds": 0}
        for key, default in (("seen", []), ("repeats", 0), ("inspected", []), ("candidates", [])):
            progress.setdefault(key, default)
        request = self.store.job(job_id)["request"]
        initial = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": encode({
                "objective": request["objective"], "constraints": request["constraints"],
                "as_of": request["as_of"], "today": today(request), "plan": plan, "passages": passages[:12],
                "instruction": "Inspect evidence, record supported and contradictory findings, then finish."})}]
        while True:
            self.store.guard(job_id, self.owner)
            # Turns outside the context window still happened; list them so a long
            # run does not repeat a read/search it can no longer see in its history.
            already_read = sorted({s["original_url"] for s in self.store.sources(job_id)})
            already_searched = sorted({a["query"]["query"] for a in self.store.artifacts(job_id, "search:")})
            reminder = [{"role": "user", "content": encode({
                **self._work_order(job_id, plan, request, progress),
                "already_read_urls": already_read,
                "already_searched_queries": already_searched,
            })}]
            message = await self.call_model(job_id, f"model:analysis:{progress['turn']}",
                                            self._fit_context(initial, progress["groups"][-3:], reminder),
                                            TOOLS)
            replies, finished = [], False
            for call in message["tool_calls"]:
                action_key = f"tool:{progress['turn']}:{call['id']}"
                result = self.store.artifact(job_id, action_key)
                if result is None:
                    try:
                        name = call["function"]["name"]
                        if name not in TOOL_MODELS:
                            raise ValueError("Unknown tool")
                        arguments = TOOL_MODELS[name].model_validate_json(call["function"]["arguments"])
                        signature = digest(encode([name, arguments.model_dump(mode="json")]))
                        if name in REPEATABLE_TOOLS and signature in progress["seen"]:
                            # Budget Guard: an identical action returns identical data, so it only burns steps.
                            progress["repeats"] += 1
                            if progress["repeats"] > 2:
                                raise BudgetExceeded("Stopped after repeated identical tool calls")
                            result = {"error": "Repeated call: this exact action was already done and its "
                                               "result is unchanged. Record a finding from passages you "
                                               "already have, or take a different action."}
                        elif name == "search_sources":
                            if progress["gap_rounds"] >= request["budget"]["max_gap_rounds"]:
                                raise BudgetExceeded("Gap-round limit reached")
                            if arguments.subquestion >= len(plan["subquestions"]):
                                raise ValueError("Unknown subquestion")
                            result = await self.do_search(job_id, arguments)
                            progress["gap_rounds"] += 1
                        elif name == "read_source":
                            result = await self.do_read(job_id, arguments.url)
                        elif name == "inspect_evidence":
                            result = {"passages": self.store.retrieve(job_id, arguments.query, limit=6)}
                            progress["inspected"].append(arguments.query)
                            progress["candidates"] = list(dict.fromkeys(
                                progress["candidates"] + [p["passage_id"] for p in result["passages"]]))[-24:]
                        elif name == "record_finding":
                            # Origin/review fields cannot be impersonated by a model.
                            if arguments.origin != "local_model":
                                raise ValueError("Invalid finding origin")
                            result = {"finding_id": self.store.save_finding(job_id, self.owner, arguments)}
                        else:
                            for gap in arguments.gaps:
                                self.issue(job_id, "Unresolved: " + gap)
                            result = {"finished": True}
                        if name in REPEATABLE_TOOLS and signature not in progress["seen"]:
                            progress["seen"].append(signature)
                        progress["invalid"] = 0
                    except BudgetExceeded as exc:
                        self.issue(job_id, str(exc))
                        result = {"error": str(exc), "finished": True}
                    except (ValueError, KeyError, ReaderError, ValidationError):
                        progress["invalid"] += 1
                        result = {"error": "Invalid tool arguments or reference. Repair once using the schema."}
                        if progress["invalid"] > 1:
                            self.issue(job_id, "Tool arguments invalid after one repair")
                            result["finished"] = True
                    self.store.put(job_id, self.owner, action_key, result)
                finished = finished or result.get("finished", False)
                replies.append({"role": "tool", "tool_call_id": call["id"], "content": encode(result)})
            progress["groups"] = (progress["groups"] + [[message] + replies])[-3:]
            progress["turn"] += 1
            self.store.put(job_id, self.owner, "analysis:progress", progress)
            if finished:
                self.store.put(job_id, self.owner, "analysis:done", True)
                return state

    async def deliver(self, state):
        job_id = state["job_id"]
        self.store.stage(job_id, self.owner, "packaging")
        questions = self.store.artifact(job_id, "plan")["subquestions"]
        findings = self.store.artifacts(job_id, "finding:")
        for index, question in enumerate(questions):
            if not any(f["subquestion"] == index and any(e["relation"] == "supports" for e in f["evidence"])
                       for f in findings):
                self.issue(job_id, "No proposed support for subquestion: " + question)
        if not self.store.sources(job_id):
            self.store.finish(job_id, self.owner, "failed", "No readable sources were recovered")
        else:
            status = "partial" if self.store.issues(job_id) else "awaiting_review"
            self.store.finish(job_id, self.owner, status)
        return state
