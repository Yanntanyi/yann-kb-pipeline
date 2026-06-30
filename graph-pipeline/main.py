"""Pipeline orchestrator — runs all 5 phases in sequence.

Usage:
  python main.py                  # run all phases from the start
  python main.py --from-phase 3   # skip phases 1-2 (load their staged results)
                                  # and run from phase 3 onward
  python main.py --from-phase 5   # only re-run graph construction

Use --from-phase when you've already run earlier phases and just want to
re-run later ones (e.g. tuning Phase 5 thresholds without re-running the
expensive LLM calls in Phases 1, 2, and 4).
"""

import argparse
import sys
import time
from datetime import timedelta

from phase1_extraction import DocumentExtractor
from phase2_normalization import EntityNormalizer
from phase3_candidate_filtering import CandidateFilter
from phase4_relationship_scoring import RelationshipScorer
from phase5_graph_construction import GraphConstructor
from neo4j_handler import Neo4jHandler


def fmt_duration(seconds: float) -> str:
    return str(timedelta(seconds=round(seconds)))


def _fresh_start():
    """Clear all staged phase outputs and wipe the Neo4j graph.

    Required when the extraction schema or relationship ontology changes: Phase 4
    resumes from its staging file (doc hashes are stable, so it would skip every
    pair and silently reuse the old ontology), and Phase 5 writes with MERGE (so
    old nodes/edges/properties would linger). A from-scratch run avoids both.
    """
    import config
    removed = 0
    for f in config.STAGING_DIR.glob("*.json"):
        f.unlink()
        removed += 1
    print(f"  Cleared {removed} staged file(s) from {config.STAGING_DIR}")
    try:
        handler = Neo4jHandler()
        handler.clear_graph()
        handler.close()
        print("  Wiped Neo4j graph (all nodes + relationships)")
    except Exception as e:
        print(f"  WARNING: could not wipe Neo4j ({e}). Clear it manually before Phase 5.")


def run_pipeline(from_phase: int = 1, fresh: bool = False):
    total_start = time.time()

    print("=" * 60)
    print("  UPS Watson Knowledge Graph Pipeline  (yann-pipeline)")
    print("=" * 60)

    if fresh:
        if from_phase != 1:
            print("ERROR: --fresh starts from scratch; it can't be combined with --from-phase > 1.")
            sys.exit(1)
        print("\n--fresh: clearing staging + wiping Neo4j for a from-scratch rebuild...")
        _fresh_start()

    if from_phase > 1:
        print(f"\nResuming from Phase {from_phase} — loading staged results...\n")

    extractions = None
    entity_mapping = None
    normalized_extractions = None
    canonical_index = None
    candidates = None
    scored_relationships = None

    # ── Phase 1: Independent extraction ──────────────────────────────────────
    extractor = DocumentExtractor()

    if from_phase <= 1:
        print("\n" + "─" * 60)
        print("PHASE 1 — Independent document extraction")
        print("─" * 60)
        t0 = time.time()
        extractions = extractor.process_all_documents()
        print(f"\nPhase 1 done in {fmt_duration(time.time() - t0)}")
    else:
        extractions = extractor.load_extractions()
        if not extractions:
            print("ERROR: No Phase 1 results found. Run from phase 1 first.")
            sys.exit(1)
        print(f"Loaded {len(extractions)} extractions from staging")

    # ── Phase 2: Global entity normalization ──────────────────────────────────
    normalizer = EntityNormalizer()

    if from_phase <= 2:
        print("\n" + "─" * 60)
        print("PHASE 2 — Global entity + semantic-field normalization")
        print("─" * 60)
        t0 = time.time()
        entity_mapping = normalizer.normalize_all_entities(extractions)
        # Curated semantic-layer fields get their own alias resolution so the
        # Component/Team node counts are exact (corpus fragments these word-level).
        component_mapping = normalizer.normalize_list_field(extractions, "components")
        team_mapping = normalizer.normalize_list_field(extractions, "teams")
        print(f"\nPhase 2 done in {fmt_duration(time.time() - t0)}")
    else:
        entity_mapping = normalizer.load_normalization()
        if not entity_mapping:
            print("ERROR: No Phase 2 results found. Run from phase 2 first.")
            sys.exit(1)
        component_mapping = normalizer.load_field_normalization("components")
        team_mapping = normalizer.load_field_normalization("teams")
        print(f"Loaded {len(entity_mapping)} entity mappings from staging")

    # Build normalized extractions and canonical index (needed by phases 3-5)
    field_mappings = {"components": component_mapping, "teams": team_mapping}
    normalized_extractions = normalizer.apply_normalization(
        extractions, entity_mapping, field_mappings
    )
    canonical_index = normalizer.build_canonical_index(normalized_extractions, entity_mapping)
    print(f"Canonical index: {len(canonical_index)} unique entities")

    # ── Phase 3: Candidate filtering ──────────────────────────────────────────
    filter_engine = CandidateFilter()

    if from_phase <= 3:
        print("\n" + "─" * 60)
        print("PHASE 3 — Candidate pair filtering")
        print("─" * 60)
        t0 = time.time()
        candidates = filter_engine.filter_candidate_pairs(
            normalized_extractions, canonical_index
        )
        print(f"\nPhase 3 done in {fmt_duration(time.time() - t0)}")
    else:
        candidates = filter_engine.load_candidates()
        if not candidates:
            print("ERROR: No Phase 3 results found. Run from phase 3 first.")
            sys.exit(1)
        print(f"Loaded {len(candidates)} candidate pairs from staging")

    # ── Phase 4: Pairwise LLM scoring ────────────────────────────────────────
    scorer = RelationshipScorer()

    if from_phase <= 4:
        print("\n" + "─" * 60)
        print("PHASE 4 — Pairwise LLM relationship scoring")
        print("─" * 60)
        t0 = time.time()
        scored_relationships = scorer.score_all_candidates(candidates)
        print(f"\nPhase 4 done in {fmt_duration(time.time() - t0)}")
    else:
        scored_relationships = scorer.load_scored_relationships()
        if not scored_relationships:
            print("ERROR: No Phase 4 results found. Run from phase 4 first.")
            sys.exit(1)
        print(f"Loaded {len(scored_relationships)} scored relationships from staging")

    # ── Phase 5: Graph construction ───────────────────────────────────────────
    print("\n" + "─" * 60)
    print("PHASE 5 — Neo4j knowledge graph construction")
    print("─" * 60)
    t0 = time.time()
    constructor = GraphConstructor()
    try:
        constructor.create_graph(normalized_extractions, scored_relationships, canonical_index)
        print(f"\nPhase 5 done in {fmt_duration(time.time() - t0)}")
    finally:
        constructor.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    total_time = time.time() - total_start
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Documents processed:   {len(extractions)}")
    print(f"  Canonical entities:    {len(canonical_index)}")
    print(f"  Candidate pairs:       {len(candidates)}")
    valid = [r for r in scored_relationships if r["relationship"]["relationship_type"] != "NONE"]
    print(f"  Valid relationships:   {len(valid)}")
    print(f"  Total runtime:         {fmt_duration(total_time)}")
    print("=" * 60)
    print("\nGraph is live in Neo4j. Connect at: bolt://localhost:7687")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="UPS Watson Knowledge Graph Pipeline"
    )
    parser.add_argument(
        "--from-phase",
        type=int,
        choices=[1, 2, 3, 4, 5],
        default=1,
        help="Phase to resume from (1 = full run, default). "
             "Phases before this number load their results from staging.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="From-scratch rebuild: clear all staging and wipe the Neo4j graph "
             "first. REQUIRED after a schema or relationship-ontology change, so "
             "Phase 4 re-scores (instead of resuming) and no stale nodes/edges remain.",
    )
    args = parser.parse_args()
    run_pipeline(from_phase=args.from_phase, fresh=args.fresh)
