"""check_facets.py — facet-coverage audit + go/no-go for the doc_metric lane.

Pure Neo4j read (no LLM, no Elasticsearch). Answers the one question that decides
whether the deterministic exact-lookup lane has anything to query on THIS corpus:
are date / status / doc_type / components / teams actually populated on the
Document nodes? Run this FIRST once Neo4j is up.

  python check_facets.py
"""
from neo4j_handler import Neo4jHandler


def pct(n, d):
    return f"{(100.0 * n / d):.0f}%" if d else "n/a"


def main():
    n = Neo4jHandler()
    try:
        total = n.query_graph("MATCH (d:Document) RETURN count(d) AS c")[0]["c"]
        print(f"Documents: {total}")
        if not total:
            print("No Document nodes — run the pipeline (main.py) first.")
            return

        pre = n.query_graph(
            """
            MATCH (d:Document)
            RETURN
              sum(CASE WHEN toLower(coalesce(d.filepath,'')) STARTS WITH 'cr/'  THEN 1 ELSE 0 END) AS cr,
              sum(CASE WHEN toLower(coalesce(d.filepath,'')) STARTS WITH 'rca/' THEN 1 ELSE 0 END) AS rca
            """
        )[0]
        print(f"  by filepath: CR={pre['cr']}  RCA={pre['rca']}  "
              f"other={total - pre['cr'] - pre['rca']}")

        print("\nFacet coverage (populated / total):")
        scalar = ("date", "status", "doc_type", "severity",
                  "root_cause", "resolution", "customer_impact")
        cov = {}
        for f in scalar:
            c = n.query_graph(
                f"MATCH (d:Document) WHERE d.{f} IS NOT NULL AND toString(d.{f}) <> '' "
                f"RETURN count(d) AS c"
            )[0]["c"]
            cov[f] = c
            print(f"  {f:16} {c}/{total}  ({pct(c, total)})")
        for f in ("components", "teams", "entities", "topics"):
            c = n.query_graph(
                f"MATCH (d:Document) WHERE size(coalesce(d.{f}, [])) > 0 "
                f"RETURN count(d) AS c"
            )[0]["c"]
            cov[f] = c
            print(f"  {f:16} {c}/{total}  ({pct(c, total)})  [list]")

        print("\nDistinct status values:")
        for r in n.query_graph(
            "MATCH (d:Document) WHERE d.status IS NOT NULL "
            "RETURN d.status AS s, count(d) AS c ORDER BY c DESC"
        ):
            print(f"  {r['c']:>4}  {r['s']}")

        print("\nDistinct doc_type values:")
        for r in n.query_graph(
            "MATCH (d:Document) WHERE d.doc_type IS NOT NULL "
            "RETURN d.doc_type AS t, count(d) AS c ORDER BY c DESC"
        ):
            print(f"  {r['c']:>4}  {r['t']}")

        print("\nDate coverage:")
        dc = n.query_graph(
            "MATCH (d:Document) WHERE d.date IS NOT NULL AND d.date <> '' "
            "RETURN count(d) AS c, min(d.date) AS mn, max(d.date) AS mx"
        )[0]
        print(f"  dated docs: {dc['c']}/{total} ({pct(dc['c'], total)})  "
              f"range {dc['mn']} .. {dc['mx']}")
        for r in n.query_graph(
            "MATCH (d:Document) WHERE d.date IS NOT NULL AND d.date <> '' "
            "RETURN substring(d.date,0,7) AS m, count(d) AS c ORDER BY c DESC LIMIT 8"
        ):
            print(f"    {r['c']:>4}  {r['m']}")

        print("\nSemantic-layer nodes:")
        for lbl in ("Component", "Team", "Entity"):
            c = n.query_graph(f"MATCH (x:{lbl}) RETURN count(x) AS c")[0]["c"]
            print(f"  {lbl}: {c}")

        print("\nVERDICT — which doc_metric features will work on this corpus:")
        def verdict(name, c, need_hi=True):
            p = 100.0 * c / total
            tag = "OK" if p >= 60 else ("PARTIAL" if p >= 20 else "SPARSE")
            print(f"  {name:22} {tag:8} ({pct(c, total)})")
        verdict("doc_type filter/group", pre["cr"] + pre["rca"])  # from filepath, always
        verdict("status filter/group", cov["status"])
        verdict("component filter/group", cov["components"])
        verdict("date-window / month", cov["date"])
        print("\n  (SPARSE date coverage is expected on the real corpus — status,"
              "\n   component and doc_type lookups still work; date-window will under-return.)")
    finally:
        n.close()


if __name__ == "__main__":
    main()
