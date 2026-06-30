"""baseline_rag.py — plain hybrid-RAG with NO graph (the comparison baseline).

This is the control group for measuring whether the knowledge graph actually
helps. It does the obvious thing: embed the question, pull the top-K documents
from Elasticsearch hybrid search (BM25 + dense), dump them into the LLM, answer.
No intent, no seeds-vs-hops, no typed edges, no traversal.

It deliberately mirrors KnowledgeGraphQuerier:
  - same answer-generation prompt style (so we compare *retrieval*, not wording),
  - same TOP_K as the graph's MAX_DOCS (so both hand the LLM the same number of
    documents — the only difference is HOW those documents were chosen),
  - same run_query() output shape (intent/seeds/path/answer/timing) so the metrics
    harness can score it with identical code.

Usage:
  python baseline_rag.py "What caused the CPD certificate outage?"
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List

import config
from es_handler import ElasticsearchHandler
from llm_client import get_llm_client

TOP_K = 10  # match ask.py MAX_DOCS so the comparison isolates the graph's effect


class FlatRagQuerier:
    """Top-K hybrid retrieval + grounded answer. The no-graph control."""

    name = "flat_rag"

    def __init__(self, top_k: int = TOP_K):
        self.es = ElasticsearchHandler()
        self.llm = get_llm_client()
        self.top_k = top_k

    def find_docs(self, query: str) -> List[Dict[str, Any]]:
        query_vector = self.llm.embed([query])[0] if config.ES_USE_DENSE else None
        return self.es.hybrid_search(query, query_vector, size=self.top_k)

    def build_context(self, hits: List[Dict[str, Any]]) -> str:
        sections = []
        for i, h in enumerate(hits, 1):
            try:
                text = (config.DOCUMENTS_DIR / h["filepath"]).read_text(encoding="utf-8")
            except Exception:
                continue
            sections.append(f"[Document {i}: {h['filepath']}]\n\n{text}")
        return ("\n\n" + "-" * 60 + "\n\n").join(sections)

    def generate_answer(self, query: str, context: str) -> str:
        prompt = f"""You are an expert on cloud infrastructure incidents. Answer the question using ONLY the documents provided.

Question: {query}

Documents:
{context}

{config.ANSWER_INSTRUCTIONS}

Answer:"""
        return self.llm.generate_text(prompt, max_tokens=4096)

    def run_query(self, query: str) -> Dict[str, Any]:
        """Same output shape as KnowledgeGraphQuerier.run_query (for scoring parity)."""
        timing: Dict[str, float] = {}
        t_start = time.perf_counter()

        t = time.perf_counter()
        hits = self.find_docs(query)
        timing["retrieve"] = time.perf_counter() - t

        t = time.perf_counter()
        context = self.build_context(hits)
        timing["context"] = time.perf_counter() - t

        t = time.perf_counter()
        answer = self.generate_answer(query, context)
        timing["answer"] = time.perf_counter() - t
        timing["total"] = time.perf_counter() - t_start

        return {
            "query": query,
            "intent": "flat_rag",            # no classification in the baseline
            "intent_description": "plain top-k retrieval (no graph)",
            "edge_priority": [],
            "seeds": [h["filepath"] for h in hits],
            "path": [
                {"filepath": h["filepath"], "edge_description": None,
                 "rel_type": None, "is_seed": True}
                for h in hits
            ],
            "answer": answer,
            "timing": {k: round(v, 3) for k, v in timing.items()},
        }

    def close(self) -> None:
        pass


if __name__ == "__main__":
    q = FlatRagQuerier()
    query = " ".join(sys.argv[1:]) or input("Question: ")
    res = q.run_query(query)
    print(f"\n[flat_rag] retrieved {len(res['path'])} docs in {res['timing']['total']}s")
    for n in res["path"]:
        print(f"  - {n['filepath']}")
    print("\nAnswer:\n")
    print(res["answer"])
