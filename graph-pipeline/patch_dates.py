"""Patch document dates in the Phase 1 staging file — no LLM, no re-ingest.

Phase 1 stored a date for only ~25 docs because it relied on the LLM and never
saw the filename. This re-applies the deterministic resolver (date_utils) to the
EXISTING staging extractions, filling in the dates the LLM missed (filename dates,
dates-in-passing). It does NOT call the LLM and does NOT touch entities, topics,
or relationships.

After running this, rebuild the graph's nodes + PRECEDED_BY temporal edges with
the real, tested pipeline — no LLM cost:

    python patch_dates.py
    python main.py --from-phase 5

`--from-phase 5` reloads staging (phases 1–4) and re-runs only Phase 5, which
reads these dates and rebuilds the temporal edges. The merge is conservative
(filename-authoritative, keep existing LLM dates, body only fills nulls), so it
can only add coverage, never regress a date the LLM already had right.
"""

import json

import config
from date_utils import resolve_date


def main() -> None:
    staging = config.STAGING_DIR / "phase1_extractions.json"
    if not staging.exists():
        print(f"No staging file at {staging}. Run Phase 1 first.")
        return

    data = json.loads(staging.read_text(encoding="utf-8"))

    before = sum(1 for r in data.values() if (r.get("extraction") or {}).get("date"))
    changed = []

    for rec in data.values():
        ex = rec.setdefault("extraction", {})
        old = ex.get("date")
        try:
            content = (config.DOCUMENTS_DIR / rec["filepath"]).read_text(
                encoding="utf-8", errors="ignore"
            )
        except Exception as e:
            print(f"  skip {rec['filepath']}: {e}")
            continue
        new = resolve_date(rec["filename"], content, old)
        if new and new != old:
            ex["date"] = new
            changed.append((rec["filepath"], old, new))

    staging.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    after = sum(1 for r in data.values() if (r.get("extraction") or {}).get("date"))
    print(f"Patched {len(changed)} dates.  Dated docs: {before} -> {after}")
    for fp, old, new in changed[:20]:
        print(f"  {old!s:>12} -> {new}   {fp}")
    if len(changed) > 20:
        print(f"  …and {len(changed) - 20} more")
    print("\nNext: python main.py --from-phase 5   (rebuilds nodes + PRECEDED_BY, no LLM)")


if __name__ == "__main__":
    main()
