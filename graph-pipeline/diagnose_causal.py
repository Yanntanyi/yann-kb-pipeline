"""diagnose_causal.py — is the sparsity of CAUSED_BY/REMEDIATED_BY/RECURRENCE_OF a
Phase 3 (candidate starvation) problem or a Phase 4 (scoring) problem?

The relational-reasoning edges live almost entirely in CR<->RCA pairs (CAUSED_BY,
REMEDIATED_BY) and RCA<->RCA pairs (RECURRENCE_OF). Phase 4 can only TYPE a pair
that Phase 3 surfaced as a candidate, so before blaming the prompt you must check
whether those pairs even reach the LLM. This script reads the staging files and
answers that directly.

How to read the output:
  - If CR-RCA / RCA-RCA candidates are a tiny fraction of all possible such pairs
    -> Phase 3 is starving the causal pairs (fix: gate CR<->RCA on a SHARED
       COMPONENT, not on >=2 shared entities). Prompt tuning would do nothing.
  - If there are plenty of CR-RCA candidates but they score NONE / PROVIDES_CONTEXT_FOR
    -> Phase 4 is the issue (the LLM is seeing the pairs but not calling the link).

Usage (run in graph-pipeline/, on the machine that holds the real staging):
  python diagnose_causal.py
  python diagnose_causal.py CPD ODLM MCOG   # also dump every scored pair touching
                                            # a doc whose path contains these terms
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import config

STAGING = config.STAGING_DIR
RELATIONAL = {"CAUSED_BY", "REMEDIATED_BY", "RECURRENCE_OF"}


def _load(name):
    p = STAGING / name
    if not p.exists():
        sys.exit(f"missing {p} — run the pipeline on this machine first.")
    return json.loads(p.read_text(encoding="utf-8"))


def kind(fp: str) -> str:
    if fp.startswith("RCA/"):
        return "RCA"
    if fp.startswith("CR/"):
        return "CR"
    return "?"


def combo(a: str, b: str) -> str:
    return "-".join(sorted([kind(a), kind(b)]))


def main():
    ext = _load("phase1_extractions.json")
    cands = _load("phase3_candidate_pairs.json")
    scored = _load("phase4_scored_relationships.json")

    # Doc inventory -> the denominator for "possible pairs of each type"
    n = collections.Counter(kind(d["filepath"]) for d in ext.values())
    possible = {
        "CR-RCA": n["CR"] * n["RCA"],
        "RCA-RCA": n["RCA"] * (n["RCA"] - 1) // 2,
        "CR-CR": n["CR"] * (n["CR"] - 1) // 2,
    }
    print(f"Documents: {n['RCA']} RCA, {n['CR']} CR\n")

    # ── Phase 3: candidate coverage by doc-type combo ─────────────────────────
    cc = collections.Counter(combo(c["filepath1"], c["filepath2"]) for c in cands)
    print("=== PHASE 3 — candidate coverage (did the causal pairs survive?) ===")
    for k in ("CR-RCA", "RCA-RCA", "CR-CR"):
        poss = possible[k] or 1
        pct = 100 * cc.get(k, 0) / poss
        flag = "  <-- causal pairs live here" if k in ("CR-RCA", "RCA-RCA") else ""
        print(f"  {k:8} {cc.get(k,0):6} candidates of {possible[k]:6} possible "
              f"({pct:5.1f}%){flag}")

    # ── Phase 4: how the surfaced pairs were actually typed ───────────────────
    by_combo = collections.defaultdict(collections.Counter)
    for r in scored:
        by_combo[combo(r["filepath1"], r["filepath2"])][
            r["relationship"]["relationship_type"]
        ] += 1
    print("\n=== PHASE 4 — relationship type among the pairs that WERE scored ===")
    for k in ("CR-RCA", "RCA-RCA", "CR-CR"):
        c = by_combo.get(k, {})
        total = sum(c.values())
        relational = sum(c.get(t, 0) for t in RELATIONAL)
        print(f"\n  {k}: {total} scored | relational (CAUSED/REMEDIATED/RECURRENCE): {relational}")
        for t, v in sorted(c.items(), key=lambda x: -x[1]):
            print(f"     {t}: {v}")

    # ── The verdict heuristic ─────────────────────────────────────────────────
    crrca_cov = 100 * cc.get("CR-RCA", 0) / (possible["CR-RCA"] or 1)
    crrca_scored = sum(by_combo.get("CR-RCA", {}).values())
    crrca_relational = sum(by_combo.get("CR-RCA", {}).get(t, 0) for t in RELATIONAL)
    print("\n=== READ ===")
    if crrca_cov < 5:
        print(f"  CR-RCA candidate coverage is {crrca_cov:.1f}% — PHASE 3 is starving the "
              "causal pairs. A prompt fix won't help; gate CR<->RCA on a shared component.")
    elif crrca_scored and crrca_relational / crrca_scored < 0.25:
        print(f"  CR-RCA pairs ARE reaching Phase 4 ({crrca_scored}) but only "
              f"{crrca_relational} got a relational type — PHASE 4 scoring is the lever.")
    else:
        print("  Causal pairs are both surfaced and typed — sparsity is likely genuine rarity.")

    # ── Optional: dump every relational edge, + spot-check by doc name ─────────
    print("\n=== every relational edge in the graph ===")
    for r in scored:
        t = r["relationship"]["relationship_type"]
        if t in RELATIONAL:
            print(f"  [{t} s{r['relationship']['strength']}] "
                  f"{r['filepath1']}  <->  {r['filepath2']}")

    terms = [a.lower() for a in sys.argv[1:]]
    if terms:
        print(f"\n=== scored pairs touching {terms} (candidate? + how it scored) ===")
        cand_pairs = {frozenset([c["filepath1"], c["filepath2"]]) for c in cands}
        for r in scored:
            f1, f2 = r["filepath1"], r["filepath2"]
            if any(term in f1.lower() or term in f2.lower() for term in terms):
                is_cand = frozenset([f1, f2]) in cand_pairs
                print(f"  [{r['relationship']['relationship_type']} "
                      f"s{r['relationship']['strength']}] cand={is_cand}  "
                      f"{f1.split('/')[-1][:38]} <-> {f2.split('/')[-1][:38]}")


if __name__ == "__main__":
    main()
