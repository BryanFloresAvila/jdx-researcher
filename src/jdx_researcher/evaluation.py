"""Offline ranking evaluation; labels are supplied by a human-reviewed corpus."""
import json
import math
from collections import defaultdict
from pathlib import Path

from .ranking import LexicalRanker, Qwen3Ranker


def dcg(labels):
    return sum((2**label - 1) / math.log2(index + 2) for index, label in enumerate(labels))


async def evaluate(path: Path, selector="lexical", top_k=5, revision="main", device="auto"):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    groups, partitions = defaultdict(list), {}
    seen = set()
    for row in rows:
        question_id = row["question_id"]
        split = row["split"]
        if split not in ("train", "calibration", "test"):
            raise ValueError("Unknown partition")
        if question_id in partitions and partitions[question_id] != split:
            raise ValueError("Question leakage across partitions")
        partitions[question_id] = split
        key = (question_id, row["passage_id"])
        if key in seen:
            raise ValueError("Duplicate labeled pair")
        seen.add(key)
        if row["label_origin"] != "human_reviewed" or row["relevance"] not in (0, 1, 2):
            raise ValueError("Evaluation requires human-reviewed relevance labels 0, 1 or 2")
        groups[question_id].append(row)
    ranker = Qwen3Ranker(revision, device) if selector == "qwen3" else LexicalRanker()
    per_question = []
    for question_id, items in groups.items():
        if len({item["query"] for item in items}) != 1:
            raise ValueError("Conflicting question text")
        labels = {item["passage_id"]: item["relevance"] for item in items}
        ranked = await ranker.rank(items[0]["query"], items)
        ordered = [labels[p["passage_id"]] for _, p in ranked[:top_k]]
        total = sum(label > 0 for label in labels.values())
        hits = sum(label > 0 for label in ordered)
        ideal = dcg(sorted(labels.values(), reverse=True)[:top_k])
        per_question.append({
            "question_id": question_id, "split": partitions[question_id],
            "relevant_retained": hits, "relevant_total": total,
            "recall": hits / total if total else None,
            "ndcg": dcg(ordered) / ideal if ideal else None,
            "pairs": len(items)})
    return {"schema_version": "1.0", "selector": selector, "top_k": top_k,
            "questions": len(groups), "pairs": len(rows), "per_question": per_question,
            "notice": "Ranking relevance only. This does not measure factual support or report quality."}
