"""diagnose_traversal.py — WHY isn't the graph retrieving the connecting doc?

For each positive relationship question, run the graph and classify what happened:
  - REACHED                 : connecting doc was retrieved (good)
  - ANCHOR_NOT_REACHED      : the incident the CR connects to wasn't even retrieved
                              -> a SEEDING problem (hybrid search didn't anchor it)
  - EDGE_EXISTS_NOT_WALKED  : a DIRECT edge to the connecting doc existed from a
                              retrieved doc, but the walk never took it
                              -> a PRIORITY / CROWDING problem (off-plan ranked too low)
  - MULTIHOP_NOT_COMPLETED  : no direct edge; the multi-hop path wasn't finished
  - THEMATIC                : routed to the aggregation path (no traversal)

The TALLY at the end tells us which fix to make. Read-only. Needs the full stack.
Run:  python diagnose_traversal.py
"""

from __future__ import annotations

import json
from pathlib import Path

from ask import KnowledgeGraphQuerier
from diagnose_edges import direct_edge

GOLD = Path(__file__).with_name("eval_relationships.jsonl")


def short(fp: str) -> str:
    return fp.split("/")[-1][:36]


def main():
    rows = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    positives = [r for r in rows if not r.get("negative")]

    q = KnowledgeGraphQuerier()
    tally = {"REACHED": 0, "ANCHOR_NOT_REACHED": 0, "EDGE_EXISTS_NOT_WALKED": 0,
             "MULTIHOP_NOT_COMPLETED": 0, "THEMATIC": 0}
    print("=" * 78)
    print("TRAVERSAL DIAGNOSTIC — why each connecting doc was or wasn't retrieved")
    print("=" * 78)
    try:
        for g in positives:
            conn = g["connecting_doc"]
            anchors = [d for d in g["must_connect"] if d != conn]
            if not conn or not anchors:
                continue
            res = q.run_query(g["question"])
            intent = res.get("intent")
            seeds = set(res.get("seeds", []))
            path = [n["filepath"] for n in res.get("path", [])]

            print(f"\n[{g['id']}] intent={intent}  ndocs={len(path)}  conn={short(conn)}")
            if conn in path:
                where = "as SEED" if conn in seeds else "via TRAVERSAL"
                print(f"   REACHED ({where}) ✓")
                tally["REACHED"] += 1
                continue
            if intent == "thematic":
                print("   routed to thematic (no traversal)")
                tally["THEMATIC"] += 1
                continue

            anchor_seeded = [a for a in anchors if a in seeds]
            anchor_in_path = [a for a in anchors if a in path]
            direct = [a for a in anchors if a and direct_edge(q.neo4j, a, conn)]
            print(f"   anchor seeded: {[short(a) for a in anchor_seeded] or 'NONE'}")
            print(f"   anchor retrieved: {[short(a) for a in anchor_in_path] or 'NONE'}")
            print(f"   direct edge anchor->conn exists: {[short(a) for a in direct] or 'NONE'}")

            if not anchor_in_path:
                print("   => ANCHOR_NOT_REACHED (seeding problem)")
                tally["ANCHOR_NOT_REACHED"] += 1
            elif direct:
                print("   => EDGE_EXISTS_NOT_WALKED (crowded out / priority too low)")
                tally["EDGE_EXISTS_NOT_WALKED"] += 1
            else:
                print("   => MULTIHOP_NOT_COMPLETED (no direct edge; path not finished)")
                tally["MULTIHOP_NOT_COMPLETED"] += 1
    finally:
        q.close()

    print("\n" + "=" * 78)
    print("TALLY — the dominant failure mode tells us the fix")
    print("=" * 78)
    for k, v in tally.items():
        print(f"  {k:<24} {v}")
    print()
    print("  ANCHOR_NOT_REACHED dominant   -> fix SEEDING (anchor incident isn't seeded)")
    print("  EDGE_EXISTS_NOT_WALKED domin. -> fix RANKING/BUDGET (off-plan edge crowded out)")
    print("  MULTIHOP_NOT_COMPLETED domin. -> fix multi-hop reach (depth/budget)")


if __name__ == "__main__":
    main()
