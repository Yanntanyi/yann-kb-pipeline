"""analyze_phase4.py — Inspect Phase 4 results to tune Phase 3 gates from data.

Reads staging/phase4_scored_relationships.json (whatever has been scored so far,
even a partial run) and reports:

  1. How many pairs are NONE vs a real relationship, and the type/strength mix
  2. Which Phase 3 gate let pairs in (entity overlap vs semantic fallback)
  3. The shared-entity-count and semantic-score distributions for NONE vs valid
  4. A "what-if" grid: if you tightened MIN_ENTITY_OVERLAP / MIN_SEMANTIC_SIMILARITY,
     how many pairs you'd stop scoring — split into noise removed (NONE) vs real
     edges lost, and how many of the lost edges were *strong* (the ones you care
     about)

No LLM or database needed. Usage:
  python analyze_phase4.py
"""

import json
from collections import Counter

import config

STRONG_STRENGTH = 7   # a "lost" edge at/above this is a real concern


def pct(values, p):
    """Simple percentile (nearest-rank) over a sorted list of numbers."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def describe(label, values):
    if not values:
        print(f"  {label:<22} (none)")
        return
    print(
        f"  {label:<22} n={len(values):<5} "
        f"min={min(values):<6.3g} p25={pct(values,25):<6.3g} "
        f"median={pct(values,50):<6.3g} p75={pct(values,75):<6.3g} "
        f"max={max(values):<6.3g}"
    )


def main():
    path = config.STAGING_DIR / "phase4_scored_relationships.json"
    if not path.exists():
        print(f"No Phase 4 results at {path}. Run Phase 4 first.")
        return

    with open(path, encoding="utf-8") as f:
        records = json.load(f)

    if not records:
        print("Phase 4 results are empty.")
        return

    # Split into valid vs NONE. Each record carries the Phase 3 evidence
    # (shared_entities, semantic_score) and the LLM verdict (relationship).
    valid, none = [], []
    for r in records:
        (valid if r["relationship"]["relationship_type"] != "NONE" else none).append(r)

    total = len(records)
    print(f"\n=== Totals ({total} pairs scored so far) ===")
    print(f"  valid relationships : {len(valid)} ({100*len(valid)/total:.0f}%)")
    print(f"  NONE (no relation)  : {len(none)} ({100*len(none)/total:.0f}%)")

    print("\n=== Which gate let pairs in ===")
    print(f"  {dict(Counter(r.get('gate', 'entity') for r in records))}")

    print("\n=== Relationship type mix (valid only) ===")
    for rtype, c in Counter(
        r["relationship"]["relationship_type"] for r in valid
    ).most_common():
        print(f"  {rtype:<22} {c}")

    print("\n=== Strength distribution (valid only) ===")
    describe("strength", [r["relationship"]["strength"] for r in valid])

    print("\n=== Phase 3 signals: NONE vs valid ===")
    print(" shared-entity count:")
    describe("NONE", [len(r["shared_entities"]) for r in none])
    describe("valid", [len(r["shared_entities"]) for r in valid])
    print(" semantic_score (only >0 = came via semantic gate):")
    describe("NONE  >0", [r["semantic_score"] for r in none if r["semantic_score"] > 0])
    describe("valid >0", [r["semantic_score"] for r in valid if r["semantic_score"] > 0])

    # ── What-if: simulate tightening the gates over already-scored pairs ──
    # A pair is KEPT if it would still pass EITHER tightened gate:
    #   shared-entity count >= O   OR   semantic_score >= S
    print("\n=== What-if: tighten gates (over already-scored pairs) ===")
    print("  O=min entity overlap, S=min semantic. 'lost' = real edges dropped,")
    print(f"  'lost_strong' = dropped edges with strength>={STRONG_STRENGTH} (the worrying ones).\n")
    header = f"  {'O':>2} {'S':>5} | {'dropped':>7} {'noise_rm':>8} {'lost':>5} {'lost_strong':>11}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for O in (2, 3, 4):
        for S in (0.25, 0.35, 0.45):
            dropped = noise_rm = lost = lost_strong = 0
            for r in records:
                count = len(r["shared_entities"])
                sem = r["semantic_score"]
                kept = (count >= O) or (sem >= S)
                if kept:
                    continue
                dropped += 1
                if r["relationship"]["relationship_type"] == "NONE":
                    noise_rm += 1
                else:
                    lost += 1
                    if r["relationship"]["strength"] >= STRONG_STRENGTH:
                        lost_strong += 1
            print(f"  {O:>2} {S:>5.2f} | {dropped:>7} {noise_rm:>8} {lost:>5} {lost_strong:>11}")

    print(
        "\nRead it like this: pick the row with the most noise_rm and the fewest "
        "lost_strong.\nThat's your sweet spot for MIN_ENTITY_OVERLAP / MIN_SEMANTIC_SIMILARITY."
    )


if __name__ == "__main__":
    main()
