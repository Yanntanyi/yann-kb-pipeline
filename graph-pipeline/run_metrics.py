"""run_metrics.py — score the graph system vs the flat-RAG baseline.

Produces the headline numbers for the meeting:
  1. Intent accuracy     — % of questions routed to an acceptable intent (graph only)
  2. Retrieval recall     — must-have-doc recall + all-required-hit rate
  3. Latency              — median/p95 total, plus the pure-graph slice
  4. Faithfulness/abstain — (with --judge) hallucination-free rate + correct abstention
  5. Lift over flat RAG   — every metric, side by side with the no-graph baseline

Deterministic metrics (intent, recall, precision, latency) need NO judge and are
fully reproducible. The --judge layer adds answer-quality grading via the LLM.

Usage (needs Neo4j + Elasticsearch + the LLM up, same as ask.py):
  python run_metrics.py                         # both systems, deterministic only
  python run_metrics.py --judge                 # + LLM answer grading
  python run_metrics.py --systems graph_traversal
  python run_metrics.py --limit 8               # quick smoke run on first 8 Qs
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

import config

GOLD = Path(__file__).with_name("eval_gold.jsonl")
RESULTS = Path(__file__).with_name("metrics_results")


# ── systems under test ────────────────────────────────────────────────────────
def build_system(key: str):
    if key == "graph_traversal":
        from ask import KnowledgeGraphQuerier
        return KnowledgeGraphQuerier()
    if key == "flat_rag":
        from baseline_rag import FlatRagQuerier
        return FlatRagQuerier()
    raise ValueError(key)


# ── deterministic scoring ─────────────────────────────────────────────────────
def score_retrieval(retrieved: List[str], must: List[str], nice: List[str]) -> Dict:
    """Recall / all-hit / precision against the gold sets (None when N/A)."""
    rset, mset = set(retrieved), set(must)
    relevant = mset | set(nice)
    if not mset:
        recall, all_hit = None, None  # not a retrieval-scored question
    else:
        hits = mset & rset
        recall = len(hits) / len(mset)
        all_hit = 1 if mset <= rset else 0
    precision = (len(rset & relevant) / len(rset)) if (rset and relevant) else None
    return {"recall": recall, "all_hit": all_hit, "precision": precision}


def intent_correct(pred_intent: str, accepted: List[str]) -> Any:
    if pred_intent in (None, "flat_rag"):
        return None  # baseline has no intent to score
    return 1 if pred_intent in accepted else 0


# ── business-lens scoring (read-wide: the graph-exclusive advantages) ──────────
# These isolate what a top-K retriever physically cannot do on corpus-wide
# questions: state an EXACT count, and ground on the WHOLE relevant set (not just
# the top 10 docs). Both are deterministic — no judge needed.

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _numbers_in(text: str) -> List[int]:
    """Every integer mentioned in the answer (digits + spelled-out one..twelve)."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\b\d[\d,]*\b", text or "")]
    low = (text or "").lower()
    nums += [v for w, v in _NUM_WORDS.items() if re.search(rf"\b{w}\b", low)]
    return nums


def score_counts(answer: str, targets: List[Dict[str, Any]]) -> Any:
    """Fraction of ground-truth counts the answer actually states (None if N/A).

    A target is hit if some number in the answer lands within its tolerance. This
    is the headline business metric: flat RAG can't count past its 10-doc window.
    """
    if not targets:
        return None
    nums = _numbers_in(answer)
    hits = sum(
        1 for t in targets
        if any(abs(n - t["value"]) <= t.get("tol", 0) for n in nums)
    )
    return round(hits / len(targets), 3)


# ── optional LLM judge (nugget-based) ─────────────────────────────────────────
def make_judge():
    from llm_client import get_llm_client
    return get_llm_client()


def judge_completeness(llm, question: str, answer: str, facts: List[str]) -> Any:
    if not facts:
        return None
    numbered = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
    prompt = (f"Grade whether the Answer states each required fact (paraphrase counts).\n\n"
              f"Question: {question}\n\nAnswer:\n\"\"\"{answer}\"\"\"\n\nRequired facts:\n{numbered}\n\n"
              f'Return ONLY JSON: {{"results":[{{"n":1,"present":true}}, ...]}}')
    try:
        out = llm.generate_json(prompt)
        pres = {r["n"]: bool(r.get("present")) for r in out.get("results", []) if "n" in r}
        return round(sum(1 for i in range(1, len(facts) + 1) if pres.get(i)) / len(facts), 3)
    except Exception:
        return None


def judge_faithful(llm, question: str, answer: str, context: str) -> Any:
    prompt = (f"Does the Answer make any factual claim NOT supported by the Sources? "
              f"An answer that says info is missing/unknown is faithful.\n\n"
              f"Question: {question}\n\nSources:\n\"\"\"{context[:12000]}\"\"\"\n\n"
              f'Answer:\n"""{answer}"""\n\nReturn ONLY JSON: {{"faithful": true|false}}')
    try:
        return 1 if bool(llm.generate_json(prompt).get("faithful", True)) else 0
    except Exception:
        return None


def judge_abstained(llm, question: str, answer: str) -> Any:
    prompt = (f"Did the Answer correctly indicate the information is unavailable/unknown/"
              f"not in the documents, rather than inventing it?\n\nQuestion: {question}\n\n"
              f'Answer:\n"""{answer}"""\n\nReturn ONLY JSON: {{"abstained": true|false}}')
    try:
        return 1 if bool(llm.generate_json(prompt).get("abstained", False)) else 0
    except Exception:
        return None


def read_context(docs_dir: Path, retrieved: List[str]) -> str:
    parts = []
    for d in retrieved:
        try:
            parts.append((docs_dir / d).read_text(encoding="utf-8"))
        except Exception:
            pass
    return "\n\n".join(parts)


# ── aggregation helpers ───────────────────────────────────────────────────────
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _pct(xs):
    xs = [x for x in xs if x is not None]
    return round(100 * statistics.mean(xs), 1) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def _p95(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[max(0, int(0.95 * len(xs)) - 1)], 3) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", nargs="+", default=["graph_traversal", "flat_rag"])
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--ids", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    gold = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.ids:
        gold = [g for g in gold if g["id"] in set(args.ids)]
    if args.limit:
        gold = gold[: args.limit]
    print(f"Loaded {len(gold)} gold questions.")

    # Make the LLM backend explicit so you can confirm it before a long run.
    prov = config.LLM_PROVIDER
    model = config.WATSONX_MODEL if prov == "watsonx" else config.LM_STUDIO_MODEL
    embed = config.WATSONX_EMBED_MODEL if prov == "watsonx" else config.LM_STUDIO_EMBED_MODEL
    print(f"LLM provider: {prov}  |  model: {model}  |  embed: {embed}")
    if prov != "watsonx":
        print("  ** NOTE: not using watsonx gpt-oss. Set LLM_PROVIDER=watsonx to use the watsonx API. **")

    docs_dir = Path(__file__).resolve().parents[1] / "docs"
    judge = make_judge() if args.judge else None
    RESULTS.mkdir(exist_ok=True)
    run = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "n": len(gold), "systems": {}}

    for skey in args.systems:
        print(f"\n=== {skey} ===")
        sysobj = build_system(skey)
        rows = []
        try:
            for g in gold:
                print(f"  [{g['id']}] {g['question'][:64]}...")
                try:
                    res = sysobj.run_query(g["question"])
                except Exception as e:
                    print(f"    !! error: {e}")
                    rows.append({"id": g["id"], "error": str(e)})
                    continue
                retrieved = [n["filepath"] for n in res.get("path", [])]
                row = {
                    "id": g["id"], "question": g["question"],
                    "category_intents": g["intents"], "intent_pred": res.get("intent"),
                    "intent_ok": intent_correct(res.get("intent"), g["intents"]),
                    **score_retrieval(retrieved, g["must_have"], g.get("nice_to_have", [])),
                    "count_acc": score_counts(res.get("answer", ""), g.get("expected_counts", [])),
                    "breadth": len(retrieved),  # docs the answer is grounded on
                    "is_thematic": "thematic" in g["intents"],
                    "latency": res.get("timing", {}).get("total"),
                    "graph_latency": res.get("timing", {}).get("traverse"),
                    "must_abstain": g["must_abstain"],
                    "n_retrieved": len(retrieved),
                    "retrieved": retrieved, "must_have": g["must_have"],
                }
                if judge is not None:
                    ctx = read_context(docs_dir, retrieved)
                    row["completeness"] = judge_completeness(judge, g["question"], res["answer"], g["key_facts"])
                    row["faithful"] = judge_faithful(judge, g["question"], res["answer"], ctx)
                    if g["must_abstain"]:
                        row["abstained"] = judge_abstained(judge, g["question"], res["answer"])
                rows.append(row)
        finally:
            if hasattr(sysobj, "close"):
                sysobj.close()
        run["systems"][skey] = rows

    stamp = time.strftime("%Y-%m-%d_%H%M")
    out = RESULTS / f"metrics_raw_{stamp}.json"
    out.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    scorecard(run)
    traversal_panel(run)
    business_panel(run)
    detail_csv, summary_csv = write_csv(run, stamp)
    print(f"\nSummary CSV  -> {summary_csv}   (the scorecard, for the meeting)")
    print(f"Detail CSV   -> {detail_csv}   (per-question, for drill-down)")
    print(f"Raw JSON     -> {out}")


def scorecard(run: Dict):
    print("\n" + "=" * 76)
    print("SCORECARD" + (" (with judge)" if any("faithful" in r for rows in run["systems"].values() for r in rows if "error" not in r) else ""))
    print("=" * 76)
    hdr = f"{'metric':<34}" + "".join(f"{s[:18]:>20}" for s in run["systems"])
    print(hdr); print("-" * len(hdr))

    def line(label, fn):
        row = f"{label:<34}"
        for s in run["systems"]:
            ok = [r for r in run["systems"][s] if "error" not in r]
            v = fn(ok)
            row += f"{('—' if v is None else v):>20}"
        print(row)

    line("Intent accuracy %", lambda ok: _pct([r["intent_ok"] for r in ok]))
    line("Must-have recall % (avg)", lambda ok: _pct([r["recall"] for r in ok]))
    line("All-required-hit rate %", lambda ok: _pct([r["all_hit"] for r in ok]))
    line("Precision % (avg)", lambda ok: _pct([r["precision"] for r in ok]))
    line("Latency median (s)", lambda ok: _median([r["latency"] for r in ok]))
    line("Latency p95 (s)", lambda ok: _p95([r["latency"] for r in ok]))
    line("Graph-walk median (s)", lambda ok: _median([r["graph_latency"] for r in ok]))
    if any("faithful" in r for rows in run["systems"].values() for r in rows if "error" not in r):
        line("Faithful % (no hallucination)", lambda ok: _pct([r.get("faithful") for r in ok]))
        line("Completeness % (key facts)", lambda ok: _pct([r.get("completeness") for r in ok]))
        line("Correct abstention %", lambda ok: _pct([r.get("abstained") for r in ok if r.get("must_abstain")]))

    n_err = {s: sum(1 for r in rows if "error" in r) for s, rows in run["systems"].items()}
    if any(n_err.values()):
        print("\nsystem errors:", {k: v for k, v in n_err.items() if v})
    print("\nNote: recall/precision computed only over questions with required docs; "
          "intent accuracy is graph-only (the baseline has no intent).")


def traversal_panel(run: Dict):
    """The read-deep scorecard: single-incident (traversal) questions only — where
    augment-not-replace applies. Isolating these from the thematic questions is the
    only way to see the augment effect; the overall scorecard blends the two and the
    fingerprint-based thematic path drags every graph number down.

    Because the graph base IS flat-RAG's top-K (plus bridges), graph recall here
    should be >= flat RAG. If it isn't, the base retrieval is not matching flat RAG.
    """
    def trav(rows):
        return [r for r in rows if "error" not in r and not r.get("is_thematic")]

    n = max((len(trav(rows)) for rows in run["systems"].values()), default=0)
    print("\n" + "=" * 76)
    print(f"TRAVERSAL PANEL — single-incident questions only, where augment applies (n={n})")
    print("=" * 76)
    hdr = f"{'metric':<34}" + "".join(f"{s[:18]:>20}" for s in run["systems"])
    print(hdr); print("-" * len(hdr))

    def line(label, fn):
        row = f"{label:<34}"
        for s in run["systems"]:
            v = fn(trav(run["systems"][s]))
            row += f"{('—' if v is None else v):>20}"
        print(row)

    line("Must-have recall % (avg)", lambda ok: _pct([r["recall"] for r in ok]))
    line("All-required-hit rate %", lambda ok: _pct([r["all_hit"] for r in ok]))
    line("Precision % (avg)", lambda ok: _pct([r["precision"] for r in ok]))
    if any("completeness" in r for rows in run["systems"].values() for r in trav(rows)):
        line("Completeness % (key facts)", lambda ok: _pct([r.get("completeness") for r in ok]))
        line("Faithful % (no hallucination)", lambda ok: _pct([r.get("faithful") for r in ok]))
    print("\nGraph base = flat-RAG top-K + bridges, so graph recall here should be >= flat RAG.")


def business_panel(run: Dict):
    """The read-wide scorecard: corpus-wide (thematic) questions only.

    This is the business persona's acceptance test — the things a top-K retriever
    structurally cannot do. Count accuracy and corpus breadth are where the graph's
    aggregation path is expected to beat the flat baseline outright.
    """
    def thematic(rows):
        return [r for r in rows if "error" not in r and r.get("is_thematic")]

    n = max((len(thematic(rows)) for rows in run["systems"].values()), default=0)
    print("\n" + "=" * 76)
    print(f"BUSINESS PANEL — corpus-wide / read-wide questions only (n={n})")
    print("=" * 76)
    hdr = f"{'metric':<34}" + "".join(f"{s[:18]:>20}" for s in run["systems"])
    print(hdr); print("-" * len(hdr))

    def line(label, fn):
        row = f"{label:<34}"
        for s in run["systems"]:
            v = fn(thematic(run["systems"][s]))
            row += f"{('—' if v is None else v):>20}"
        print(row)

    line("Count accuracy % (exact nums)", lambda ok: _pct([r.get("count_acc") for r in ok]))
    line("Corpus breadth (median docs)", lambda ok: _median([r.get("breadth") for r in ok]))
    line("Corpus breadth (max docs)", lambda ok: max([r.get("breadth", 0) for r in ok] or [None]))
    line("Must-have recall % (avg)", lambda ok: _pct([r["recall"] for r in ok]))
    line("All-required-hit rate %", lambda ok: _pct([r["all_hit"] for r in ok]))
    if any("completeness" in r for rows in run["systems"].values() for r in thematic(rows)):
        line("Completeness % (key facts)", lambda ok: _pct([r.get("completeness") for r in ok]))
        line("Faithful % (no hallucination)", lambda ok: _pct([r.get("faithful") for r in ok]))
    print("\nCount accuracy & corpus breadth are the graph-exclusive read-wide metrics: "
          "flat RAG is capped at its top-K window, so it cannot count corpus-wide or "
          "ground on the full relevant set. (Count match is numeric-within-tolerance; "
          "treat as directional.)")


# ── CSV export ────────────────────────────────────────────────────────────────
# Aggregate metrics, defined once so the summary CSV and the scorecard agree.
SUMMARY_METRICS = [
    ("intent_accuracy_pct",   lambda ok: _pct([r["intent_ok"] for r in ok])),
    ("count_accuracy_pct",    lambda ok: _pct([r.get("count_acc") for r in ok])),
    ("corpus_breadth_median", lambda ok: _median([r.get("breadth") for r in ok])),
    ("must_have_recall_pct",  lambda ok: _pct([r["recall"] for r in ok])),
    ("all_required_hit_pct",  lambda ok: _pct([r["all_hit"] for r in ok])),
    ("precision_pct",         lambda ok: _pct([r["precision"] for r in ok])),
    ("latency_median_s",      lambda ok: _median([r["latency"] for r in ok])),
    ("latency_p95_s",         lambda ok: _p95([r["latency"] for r in ok])),
    ("graph_walk_median_s",   lambda ok: _median([r["graph_latency"] for r in ok])),
    ("faithful_pct",          lambda ok: _pct([r.get("faithful") for r in ok])),
    ("completeness_pct",      lambda ok: _pct([r.get("completeness") for r in ok])),
    ("correct_abstention_pct", lambda ok: _pct([r.get("abstained") for r in ok if r.get("must_abstain")])),
]

DETAIL_COLS = [
    "system", "id", "question", "expected_intents", "predicted_intent", "intent_ok",
    "is_thematic", "count_acc", "breadth",
    "recall", "all_hit", "precision", "latency_s", "graph_walk_s", "must_abstain",
    "faithful", "completeness", "abstained", "n_retrieved", "retrieved_docs",
    "must_have_docs", "error",
]


def write_csv(run: Dict, stamp: str):
    """Write a per-question detail CSV and a one-row-per-metric summary CSV."""
    detail = RESULTS / f"metrics_detail_{stamp}.csv"
    with detail.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(DETAIL_COLS)
        for sysname, rows in run["systems"].items():
            for r in rows:
                if "error" in r:
                    w.writerow([sysname, r.get("id", "")] + [""] * (len(DETAIL_COLS) - 3) + [r["error"]])
                    continue
                w.writerow([
                    sysname, r["id"], r.get("question", ""),
                    "|".join(r.get("category_intents", [])), r.get("intent_pred", ""),
                    r.get("intent_ok"), r.get("is_thematic"), r.get("count_acc"), r.get("breadth"),
                    r.get("recall"), r.get("all_hit"), r.get("precision"),
                    r.get("latency"), r.get("graph_latency"), r.get("must_abstain"),
                    r.get("faithful"), r.get("completeness"), r.get("abstained"),
                    r.get("n_retrieved"), "|".join(r.get("retrieved", [])),
                    "|".join(r.get("must_have", [])), "",
                ])

    summary = RESULTS / f"metrics_summary_{stamp}.csv"
    systems = list(run["systems"])
    with summary.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric"] + systems)
        for name, fn in SUMMARY_METRICS:
            cells = []
            for s in systems:
                ok = [r for r in run["systems"][s] if "error" not in r]
                v = fn(ok)
                cells.append("" if v is None else v)
            w.writerow([name] + cells)
    return detail, summary


if __name__ == "__main__":
    main()
