"""smoke_doc_metric.py — end-to-end smoke test of the doc_metric exact-lookup lane.

Needs Neo4j + Elasticsearch + the LLM up (same requirements as ask.py). Runs a few
representative count / temporal / superlative questions and prints, for each: the
routed intent, the thematic plan (mode + filters + group_by), how many docs backed
the answer, timing, and the answer text. Use it to confirm the lane actually fires
(mode == "doc_metric") and returns exact numbers before running the full eval.

  python smoke_doc_metric.py
"""
from ask import KnowledgeGraphQuerier

QUESTIONS = [
    "How many change requests are still open?",
    "How many incidents happened in October 2025?",
    "Which month had the most changes?",
    "List all certificate-related changes.",
    "How many RCAs are there in total?",
    "Which component was affected by the most incidents?",
]


def main():
    q = KnowledgeGraphQuerier()
    try:
        for question in QUESTIONS:
            print("=" * 72)
            print("Q:", question)
            try:
                res = q.run_query(question)
            except Exception as e:
                print("  ERROR:", repr(e))
                continue
            print("  intent:", res.get("intent"))
            plan = res.get("thematic_plan") or {}
            if plan:
                print(f"  plan:   mode={plan.get('mode')} "
                      f"filters={plan.get('filters')} group_by={plan.get('group_by')}")
            print("  docs in path:", len(res.get("path", [])))
            print("  timing:", res.get("timing"))
            ans = (res.get("answer") or "").strip()
            print("  answer:", ans[:600])
            print()
    finally:
        q.close()


if __name__ == "__main__":
    main()
