"""diagnose_rebuild.py — did the rebuild actually produce what we intended?

Run on the machine that holds the live Neo4j graph. Answers, in order:
  1. Was the graph wiped? (any RETIRED edge type present = --fresh was NOT used,
     so you're reading a mixed old+new graph)
  2. Did the semantic layer build? (Component / Team node counts)
  3. Are the rich incident fields actually on the Document nodes, or mostly null?
     (this is the #1 suspect for "business completeness didn't improve")
  4. Do the business pivots return real counts? (top Teams / Components by docs)

Then separately, point it at the metrics raw JSON to see the 3 erroring questions:
  python diagnose_rebuild.py                         # graph introspection
  python diagnose_rebuild.py metrics_results/metrics_raw_<stamp>.json   # + errors
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from neo4j_handler import Neo4jHandler

RETIRED = ["SHARES_DOMAIN_WITH", "IMPLEMENTS", "EXTENDS", "SUPPORTS", "CONTRADICTS"]
RICH_FIELDS = ["doc_type", "root_cause", "resolution", "status",
               "customer_impact", "components", "teams"]


def main():
    neo = Neo4jHandler()
    q = neo.query_graph

    print("=== 1. NODE COUNTS BY LABEL ===")
    for label in ("Document", "Entity", "Component", "Team"):
        c = q(f"MATCH (n:{label}) RETURN count(n) AS c")
        print(f"  {label:10} {c[0]['c'] if c else 0}")

    print("\n=== 2. EDGE COUNTS BY TYPE  (retired types should be ABSENT) ===")
    rows = q("MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c ORDER BY c DESC")
    for r in rows:
        flag = "   <-- RETIRED: graph was NOT wiped!" if r["t"] in RETIRED else ""
        print(f"  {r['t']:22} {r['c']}{flag}")

    print("\n=== 3. RICH-FIELD COVERAGE ON DOCUMENT NODES (the key check) ===")
    total = q("MATCH (d:Document) RETURN count(d) AS c")[0]["c"] or 1
    for f in RICH_FIELDS:
        # list fields: non-empty list; scalar fields: non-null
        nonnull = q(
            f"MATCH (d:Document) WHERE d.{f} IS NOT NULL "
            f"AND (NOT d.{f} IN [[], '']) RETURN count(d) AS c"
        )[0]["c"]
        bar = "#" * int(40 * nonnull / total)
        print(f"  {f:16} {nonnull:4}/{total} {100*nonnull/total:5.0f}%  {bar}")
    print("  (if root_cause/resolution/customer_impact are mostly empty, that's why")
    print("   business completeness didn't improve — the synthesis has nothing to read.)")

    print("\n=== 4. BUSINESS PIVOTS — exact counts over the semantic layer ===")
    for label, edge in (("Team", "INVOLVED"), ("Component", "AFFECTS")):
        print(f"\n  Top {label}s by document count:")
        rows = q(
            f"MATCH (d:Document)-[:{edge}]->(x:{label}) "
            f"RETURN x.name AS name, count(DISTINCT d) AS n "
            f"ORDER BY n DESC, name LIMIT 8"
        )
        if not rows:
            print(f"    (none — {label} layer is empty)")
        for r in rows:
            print(f"    {r['n']:4}  {r['name']}")

    neo.close()

    # ── Optional: surface the erroring metrics questions ──────────────────────
    raw = next((a for a in sys.argv[1:] if a.endswith(".json")), None)
    if raw and Path(raw).exists():
        print(f"\n=== 5. ERRORING QUESTIONS in {raw} ===")
        run = json.loads(Path(raw).read_text(encoding="utf-8"))
        for sysname, rows in run.get("systems", {}).items():
            for r in rows:
                if "error" in r:
                    print(f"  [{sysname}] {r.get('id','?')}: {r['error']}")


if __name__ == "__main__":
    main()
