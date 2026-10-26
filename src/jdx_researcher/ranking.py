"""Relevance selection with per-question coverage and source diversity."""
import asyncio
import math
import re


def lexical_score(query, text):
    words = set(re.findall(r"\w{2,}", query.casefold()))
    values = set(re.findall(r"\w{2,}", text.casefold()))
    return len(words & values) / math.sqrt(max(1, len(words)) * max(1, len(values)))


class LexicalRanker:
    name = "lexical"

    async def rank(self, query, passages):
        return sorted(((lexical_score(query, p["text"]), p) for p in passages),
                      key=lambda item: item[0], reverse=True)


class Qwen3Ranker:
    name = "Qwen/Qwen3-Reranker-0.6B"

    def __init__(self, revision="main", device="auto"):
        self.revision, self.device = revision, device
        self.model = self.tokenizer = None
        self.lock = asyncio.Lock()

    def _rank(self, query, passages):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if self.model is None:
            device = self.device
            if device == "auto":
                device = "mps" if torch.backends.mps.is_available() else "cpu"
            self.device = device
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.name, revision=self.revision, padding_side="left", trust_remote_code=False)
            self.model = AutoModelForCausalLM.from_pretrained(
                self.name, revision=self.revision, trust_remote_code=False).to(device).eval()
        tokenizer = self.tokenizer
        prefix = ('<|im_start|>system\nJudge whether the Document meets the requirements '
                  'based on the Query and the Instruct provided. Note that the answer can only '
                  'be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        yes, no = tokenizer.convert_tokens_to_ids("yes"), tokenizer.convert_tokens_to_ids("no")
        values = []
        for passage in passages:
            header = ("<Instruct>: Given a research question, retrieve relevant passages\n"
                      "<Query>: " + query + "\n<Document>: ")
            before = tokenizer.encode(prefix + header, add_special_tokens=False)
            after = tokenizer.encode(suffix, add_special_tokens=False)
            available = 4096 - len(before) - len(after)
            if available <= 0:
                raise ValueError("Reranker query leaves no document context")
            body = tokenizer.encode(passage["text"], add_special_tokens=False)[:available]
            tokens = torch.tensor([before + body + after], device=self.device)
            with torch.inference_mode():
                logits = self.model(input_ids=tokens, attention_mask=torch.ones_like(tokens)).logits[0, -1]
                score = logits[[no, yes]].float().softmax(dim=-1)[1].item()
            values.append((score, passage))
        return sorted(values, key=lambda item: item[0], reverse=True)

    async def rank(self, query, passages):
        # One model operation at a time, including first load.
        async with self.lock:
            return await asyncio.to_thread(self._rank, query, passages)


async def select(store, job_id, questions, ranker, per_question=3):
    selected, decisions = {}, []
    for index, question in enumerate(questions):
        candidates = store.retrieve(job_id, question, limit=24)
        ranked = await ranker.rank(question, candidates)
        counts, retained = {}, 0
        for score, passage in ranked:
            if ranker.name == "lexical" and score <= 0:
                continue
            source_id = passage["source_id"]
            if counts.get(source_id, 0) >= 2:
                continue
            selected[passage["passage_id"]] = passage
            counts[source_id] = counts.get(source_id, 0) + 1
            decisions.append({"subquestion": index, "passage_id": passage["passage_id"],
                              "score": score, "selector": ranker.name})
            retained += 1
            if retained >= per_question:
                break
        # Expose available limitations without asserting they are contradictions.
        for _, passage in ranked:
            if re.search(r"\b(limitations?|caveats?|limitations?|limitaci[oó]n|limitaciones|unsupported)\b",
                         passage["text"], re.IGNORECASE):
                selected[passage["passage_id"]] = passage
                break
    return list(selected.values()), decisions
