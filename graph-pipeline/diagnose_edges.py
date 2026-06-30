"""diagnose_edges.py — does the graph actually CONTAIN the relationships we test?

The relationship eval showed the graph retrieving the connecting document 0% of
the time on the hard cases. There are two very different explanations:
  (1) the edge was never built  -> a Phase 3/4 problem (similarity-gated it out)
  (2) the edge exists but wasn't traversed -> an intent/edge-plan/hop problem

This script settles it. For every positive relationship question it looks up, in
Neo4j, whether the connecting document is linked to the incident it should connect
to — as a DIRECT document-to-document edge, via a multi-hop PATH (<=4 hops over
semantic/temporal edges), or NOT AT ALL.

Read-only. Needs Neo4j up. Run:  python diagnose_edges.py
"""

from __future__ import annotations

import json
from pathlib import Path

from neo4j_handler import Neo4jHandler

GOLD = Path(__file__).with_name("eval_relationships.jsonl")

# Document<->Document edge types only (exclude MENTIONS, which bridges via Entity
# nodes — we want to know if a real relationship edge exists, not an entity overlap).
DOC_RELS = ("CAUSED_BY|REMEDIATED_BY|RECURRENCE_OF|REFERENCES|"
            "PROVIDES_CONTEXT_FOR|PRECEDED_BY|RELATED_TO")


def node_exists(neo4j, fp: str) -> bool:
    r = neo4j.query_graph("MATCH (d:Document {filepath:$fp}) RETURN count(d) AS c", fp=fp)
    return bool(r) and r[0]["c"] > 0


def direct_edge(neo4j, a: str, b: str):
    """Strongest direct doc-to-doc edge between a and b (either direction), or None."""
    r = neo4j.query_graph(
        "MATCH (a:Document {filepath:$a})-[rel]-(b:Document {filepath:$b}) "
        "RETURN type(rel) AS type, coalesce(rel.strength, '') AS strength "
        "ORDER BY rel.strength DESC LIMIT 1",
        a=a, b=b)
    return r[0] if r else None


def shortest(neo4j, a: str, b: str, maxhops: int = 4):
    """Shortest doc-to-doc path (<=maxhops) over real relationship edges, or None."""
    r = neo4j.query_graph(
        "MATCH (a:Document {filepath:$a}), (b:Document {filepath:$b}) "
        f"MATCH p = shortestPath((a)-[:{DOC_RELS}*..{maxhops}]-(b)) "
        "RETURN length(p) AS hops, [x IN relationships(p) | type(x)] AS types, "
        "[n IN nodes(p) | n.filepath] AS nodes LIMIT 1",
        a=a, b=b)
    return r[0] if r else None


def short(fp: str) -> str:
    return fp.split("/")[-1][:42]


def main():
    rows = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]
    positives = [r for r in rows if not r.get("negative")]

    neo4j = Neo4jHandler()
    counts = {"DIRECT": 0, "PATH": 0, "NONE": 0, "MISSING_NODE": 0, "NA": 0}
    print("=" * 78)
    print("EDGE DIAGNOSTIC — is each tested relationship actually in the graph?")
    print("=" * 78)

    try:
        for q in positives:
            conn = q["connecting_doc"]
            anchors = [d for d in q["must_connect"] if d != conn]
            print(f"\n[{q['id']}] {q['question'][:62]}")
            if not conn or not anchors:
                print("   (single-document question — no doc-to-doc edge to check)  N/A")
                counts["NA"] += 1
                continue

            best = "NONE"
            for anchor in anchors:
                if not node_exists(neo4j, conn):
                    print(f"   !! connecting doc NOT in graph: {short(conn)}")
                    best = "MISSING_NODE"; break
                if not node_exists(neo4j, anchor):
                    print(f"   !! anchor doc NOT in graph: {short(anchor)}")
                    best = "MISSING_NODE"; break

                d = direct_edge(neo4j, conn, anchor)
                if d:
                    print(f"   {short(conn)}  <->  {short(anchor)}")
                    print(f"      DIRECT edge: {d['type']} (strength {d['strength']})")
                    best = "DIRECT"
                    continue
                p = shortest(neo4j, conn, anchor)
                if p:
                    print(f"   {short(conn)}  <->  {short(anchor)}")
                    print(f"      no direct edge; PATH {p['hops']} hops via {p['types']}")
                    if best != "DIRECT":
                        best = "PATH"
                else:
                    print(f"   {short(conn)}  <->  {short(anchor)}")
                    print(f"      NO connection within 4 hops")

            verdict = {"DIRECT": "DIRECT EDGE ✓", "PATH": "PATH ONLY (multi-hop)",
                       "NONE": "NO CONNECTION (edge never built) ✗",
                       "MISSING_NODE": "DOC MISSING FROM GRAPH ✗"}[best]
            print(f"   => {verdict}")
            counts[best] += 1
    finally:
        neo4j.close()

    print("\n" + "=" * 78)
    print("SUMMARY  (of the connecting relationships the eval tests)")
    print("=" * 78)
    total = sum(counts.values())
    print(f"  DIRECT edge exists:        {counts['DIRECT']:>3} / {total}")
    print(f"  PATH only (multi-hop):     {counts['PATH']:>3} / {total}")
    print(f"  NO connection (<=4 hops):  {counts['NONE']:>3} / {total}")
    print(f"  Doc missing from graph:    {counts['MISSING_NODE']:>3} / {total}")
    print(f"  N/A (single-doc question): {counts['NA']:>3} / {total}")
    print()
    print("How to read it:")
    print("  - Many NO-CONNECTION  => the edges were never built (Phase 3 similarity-gated")
    print("    them out). The graph can't traverse a link it never learned. BUILD problem.")
    print("  - Mostly DIRECT/PATH but the eval still got 0% retrieval => the edges exist but")
    print("    aren't being followed (intent edge-plan / hops / seeds). TRAVERSAL problem.")


if __name__ == "__main__":
    main()
