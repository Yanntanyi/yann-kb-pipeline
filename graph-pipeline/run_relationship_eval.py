"""run_relationship_eval.py — prove the graph connects documents, flat RAG can't.

Two axes per system, on the relationship gold set (eval_relationships.jsonl):
  1. Connecting-doc retrieval — did it RETRIEVE the (often low-similarity) doc that
     holds the other end of the relationship? (flat RAG can't connect what it never found)
  2. Relationship accuracy    — did the ANSWER correctly STATE the cross-doc connection?
     (judged: yes=1.0 / partial=0.5 / no=0.0)

It also prints the 2x2 — split by whether flat RAG retrieved the connecting doc — so
you can say: "where flat misses the connecting doc, the graph retrieves it X% and
states the relationship Y%, vs flat Z%." And it writes a side-by-side answer dump
(the most persuasive artifact for the meeting).

Usage (needs Neo4j + Elasticsearch + the LLM):
  python run_relationship_eval.py
  python run_relationship_eval.py --no-judge      # retrieval axis only (no LLM grading)
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any, Dict

import config

GOLD = Path(__file__).with_name("eval_relationships.jsonl")
RESULTS = Path(__file__).with_name("metrics_results")


def build_system(key: str):
    if key == "graph_traversal":
        from ask import KnowledgeGraphQuerier
        return KnowledgeGraphQuerier()
    if key == "flat_rag":
        from baseline_rag import FlatRagQuerier
        return FlatRagQuerier()
    raise ValueError(key)


def judge_relationship(llm, question: str, required: str, answer: str) -> Any:
    prompt = f"""You are checking whether an answer correctly states a specific cross-document relationship.

Question: {question}

REQUIRED relationship the answer must express (this may be that two things ARE
connected in a specific way, OR that they are NOT connected at all):
{required}

Answer:
\"\"\"{answer}\"\"\"

Does the answer correctly express the REQUIRED relationship?
- "yes": it clearly expresses it (states the specific connection — or, for a "no link" case, correctly says they are unrelated).
- "partial": it mentions the elements but does not clearly land the required relationship.
- "no": it gets the relationship wrong or misses it (including INVENTING a link the required relationship says does not exist).

Return ONLY JSON: {{"verdict": "yes" | "partial" | "no"}}"""
    try:
        v = str(llm.generate_json(prompt).get("verdict", "")).lower().strip()
        return {"yes": 1.0, "partial": 0.5, "no": 0.0}.get(v)
    except Exception:
        return None


def _pct(xs):
    xs = [x for x in xs if x is not None]
    return round(100 * statistics.mean(xs), 1) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--systems", nargs="+", default=["graph_traversal", "flat_rag"])
    args = ap.parse_args()

    gold = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"Loaded {len(gold)} relationship questions.")
    prov = config.LLM_PROVIDER
    model = config.WATSONX_MODEL if prov == "watsonx" else config.LM_STUDIO_MODEL
    print(f"LLM provider: {prov}  |  model: {model}")

    llm = None
    if not args.no_judge:
        from llm_client import get_llm_client
        llm = get_llm_client()

    # results[qid][system] = row
    results: Dict[str, Dict[str, Dict]] = {g["id"]: {} for g in gold}
    answers: Dict[str, Dict[str, str]] = {g["id"]: {} for g in gold}

    for skey in args.systems:
        print(f"\n=== {skey} ===")
        sysobj = build_system(skey)
        try:
            for g in gold:
                print(f"  [{g['id']}] {g['question'][:60]}...")
                try:
                    res = sysobj.run_query(g["question"])
                except Exception as e:
                    results[g["id"]][skey] = {"error": str(e)}
                    continue
                retrieved = {n["filepath"] for n in res.get("path", [])}
                rel = (judge_relationship(llm, g["question"], g["required_relationship"], res["answer"])
                       if llm else None)
                results[g["id"]][skey] = {
                    "connecting_hit": 1 if g["connecting_doc"] in retrieved else 0,
                    "all_connected": 1 if set(g["must_connect"]) <= retrieved else 0,
                    "rel": rel,
                    "intent": res.get("intent"),
                    "n_retrieved": len(res.get("path", [])),
                }
                answers[g["id"]][skey] = res["answer"]
        finally:
            if hasattr(sysobj, "close"):
                sysobj.close()

    scorecard(gold, results, args.systems)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    RESULTS.mkdir(exist_ok=True)
    raw = RESULTS / f"relationship_results_{stamp}.json"
    raw.write_text(json.dumps({"results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    dump = write_answer_dump(gold, results, answers, stamp)
    print(f"\nSide-by-side answers -> {dump}   (the artifact for the meeting)")
    print(f"Raw JSON              -> {raw}")


def scorecard(gold, results, systems):
    print("\n" + "=" * 78)
    print("RELATIONSHIP EVAL — did it RETRIEVE the connecting doc, and STATE the link?")
    print("=" * 78)
    positives = [g for g in gold if not g.get("negative")]
    negatives = [g for g in gold if g.get("negative")]

    # per-question
    print(f"{'id':<6}{'question':<45}" + "".join(f"{s[:14]:>14}" for s in systems))
    for g in gold:
        line = f"{g['id']:<6}{g['question'][:43]:<45}"
        for s in systems:
            r = results[g["id"]].get(s, {})
            if "error" in r:
                line += f"{'ERR':>14}"
            else:
                rel = "-" if r["rel"] is None else f"{r['rel']:.1f}"
                mark = "neg" if g.get("negative") else ("C" if r["connecting_hit"] else "·")
                line += f"{mark + ' rel=' + rel:>14}"
        print(line)
    print("  legend: 'C' = retrieved the connecting doc; 'neg' = negative control; "
          "rel = relationship score (1/.5/0). For negatives, rel=1 means it correctly said 'no link'.")

    # aggregates (positive-link metrics over positives; false-link resistance over negatives)
    print("\n" + "-" * 78)
    print(f"{'metric':<40}" + "".join(f"{s[:16]:>18}" for s in systems))
    def agg(label, key, subset):
        row = f"{label:<40}"
        for s in systems:
            vals = [results[g["id"]][s].get(key) for g in subset
                    if s in results[g["id"]] and "error" not in results[g["id"]][s]]
            row += f"{str(_pct(vals)):>18}"
        print(row)
    agg("Connecting-doc retrieval % (pos)", "connecting_hit", positives)
    agg("All-connected retrieval % (pos)", "all_connected", positives)
    agg("Relationship accuracy % (pos)", "rel", positives)
    if negatives:
        agg("False-link resistance % (neg)", "rel", negatives)

    # ROUTING — what intent did the graph send each question to? (thematic SKIPS the walk)
    if "graph_traversal" in systems:
        from collections import Counter
        rows = [results[g["id"]]["graph_traversal"] for g in gold
                if "graph_traversal" in results[g["id"]]
                and "error" not in results[g["id"]]["graph_traversal"]]
        intents = Counter(r.get("intent") for r in rows)
        print("\n" + "-" * 78)
        print("GRAPH ROUTING — intent each relationship question was classified as:")
        for it, c in intents.most_common():
            tag = "   <-- SKIPS TRAVERSAL (aggregation path; the edge fix doesn't apply)" \
                  if it == "thematic" else ""
            print(f"    {str(it):<12} {c:>2}{tag}")
        ndocs = [r.get("n_retrieved") for r in rows if r.get("n_retrieved") is not None]
        if ndocs:
            print(f"    avg docs retrieved by graph: {round(sum(ndocs)/len(ndocs), 1)}")

    # the 2x2: split positive questions by whether flat retrieved the connecting doc
    if "flat_rag" in systems and "graph_traversal" in systems:
        miss = [g for g in positives
                if results[g["id"]].get("flat_rag", {}).get("connecting_hit") == 0]
        got = [g for g in positives
               if results[g["id"]].get("flat_rag", {}).get("connecting_hit") == 1]
        print("\n" + "-" * 78)
        print("THE ISOLATION CUT — positive questions where FLAT RAG missed the connecting doc:")
        print(f"  ({len(miss)} of {len(positives)} positive questions — the vocabulary-mismatch links)")
        def sub(subset, s, key):
            return _pct([results[g["id"]][s].get(key) for g in subset
                         if s in results[g["id"]] and "error" not in results[g["id"]][s]])
        if miss:
            print(f"    graph retrieved the connecting doc:   {sub(miss,'graph_traversal','connecting_hit')}%")
            print(f"    graph relationship accuracy:          {sub(miss,'graph_traversal','rel')}%")
            print(f"    flat  relationship accuracy:          {sub(miss,'flat_rag','rel')}%   (can't connect what it didn't retrieve)")
        if got:
            print(f"\n  Where flat DID retrieve the connecting doc ({len(got)} questions — same materials):")
            print(f"    graph relationship accuracy:          {sub(got,'graph_traversal','rel')}%")
            print(f"    flat  relationship accuracy:          {sub(got,'flat_rag','rel')}%")


def write_answer_dump(gold, results, answers, stamp):
    path = RESULTS / f"relationship_answers_{stamp}.md"
    lines = ["# Relationship eval — side-by-side answers", ""]
    for g in gold:
        lines.append(f"## {g['id']} — {g['question']}")
        lines.append(f"*Required relationship:* {g['required_relationship']}\n")
        for s in ("graph_traversal", "flat_rag"):
            r = results[g["id"]].get(s, {})
            tag = (f"connecting-doc {'RETRIEVED' if r.get('connecting_hit') else 'MISSED'}, "
                   f"relationship={r.get('rel')}") if "error" not in r else "ERROR"
            lines.append(f"**{s}** ({tag}):")
            lines.append("> " + (answers[g["id"]].get(s, "(no answer)").replace("\n", "\n> ")))
            lines.append("")
        lines.append("---\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


if __name__ == "__main__":
    main()
