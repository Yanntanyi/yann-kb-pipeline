"""Phase 4: Pairwise LLM relationship scoring.

For each candidate pair produced by Phase 3, reads both full documents and asks
the LLM to determine the type, strength, direction, and confidence of the
relationship between them.

Key design choices carried over from suhas-pipeline:
  - Randomised processing order to prevent any ordering bias in LLM judgements
  - Full document content passed to the LLM (not chunks) so it sees the whole story
  - NONE relationship type used as a conservative rejection — Phase 5 drops these
  - Typed relationships: EXTENDS, CONTRADICTS, SUPPORTS, REFERENCES,
    PROVIDES_CONTEXT_FOR, SHARES_DOMAIN_WITH, IMPLEMENTS, NONE

New in yann-pipeline:
  - Incremental saving every SAVE_INTERVAL pairs — if the process crashes or the
    LLM errors mid-run you keep everything scored so far and can resume
  - 'gate' and 'semantic_score' fields from Phase 3 carried through so you can
    see which gate sourced each relationship in the final graph
  - Cleaner progress reporting (running valid/NONE counts)
"""

import json
import random
from pathlib import Path
from typing import Dict, List

from llm_client import LMStudioClient
import config

# Save progress to disk every this many pairs
SAVE_INTERVAL = 10


class RelationshipScorer:
    """Score relationships between candidate document pairs using the LLM."""

    def __init__(self):
        self.llm = LMStudioClient()
        self.staging_file = config.STAGING_DIR / "phase4_scored_relationships.json"

    # ── Document reading ──────────────────────────────────────────────────────

    def read_document(self, filepath: str) -> str:
        """Read the full text of a document by its relative filepath."""
        full_path = config.DOCUMENTS_DIR / filepath
        return full_path.read_text(encoding="utf-8")

    # ── LLM scoring ───────────────────────────────────────────────────────────

    def score_relationship(
        self,
        doc1_content: str,
        doc2_content: str,
        filepath1: str,
        filepath2: str,
        shared_entities: List[str],
    ) -> Dict:
        """Ask the LLM to evaluate the relationship between two documents."""
        entities_str = ", ".join(shared_entities) if shared_entities else "none identified"

        prompt = f"""Analyze the relationship between these two documents. They share these entities: {entities_str}

DOCUMENT 1 ({filepath1}):
{doc1_content}

---

DOCUMENT 2 ({filepath2}):
{doc2_content}

---

Evaluate their relationship and return ONLY valid JSON:
{{
  "relationship_type": "one of: EXTENDS, CONTRADICTS, SUPPORTS, REFERENCES, PROVIDES_CONTEXT_FOR, SHARES_DOMAIN_WITH, IMPLEMENTS, NONE",
  "strength": <integer 1-10, where 10 is strongest relationship>,
  "description": "one sentence explaining the relationship",
  "directionality": "one of: symmetric, doc1_to_doc2, doc2_to_doc1",
  "confidence": "one of: high, medium, low"
}}

Guidelines:
- EXTENDS: One document builds upon or extends concepts from the other
- CONTRADICTS: Documents present conflicting information or viewpoints
- SUPPORTS: One document provides evidence or support for the other
- REFERENCES: One document explicitly mentions or cites the other
- PROVIDES_CONTEXT_FOR: One document gives background needed to understand the other
- SHARES_DOMAIN_WITH: Documents cover the same domain but don't directly relate
- IMPLEMENTS: One document implements ideas/concepts from the other
- NONE: No meaningful relationship despite shared entities

Use NONE if the relationship is weak or incidental. Be conservative with high scores.

Return ONLY the JSON object, no explanations."""

        try:
            result = self.llm.generate_json(prompt)

            required_fields = [
                "relationship_type",
                "strength",
                "description",
                "directionality",
                "confidence",
            ]
            for field in required_fields:
                if field not in result:
                    raise ValueError(f"Missing required field: {field}")

            strength = int(result["strength"])
            if not 1 <= strength <= 10:
                raise ValueError(f"Strength must be 1-10, got {strength}")
            result["strength"] = strength

            return result

        except Exception as e:
            print(f"  Error scoring relationship: {str(e)}")
            return {
                "relationship_type": "NONE",
                "strength": 1,
                "description": "Error during scoring",
                "directionality": "symmetric",
                "confidence": "low",
            }

    # ── Main scoring loop ─────────────────────────────────────────────────────

    def score_all_candidates(self, candidates: List[Dict]) -> List[Dict]:
        """Score every candidate pair, saving progress every SAVE_INTERVAL pairs."""
        if not candidates:
            print("No candidates to score")
            return []

        print(f"Scoring {len(candidates)} candidate pairs...")
        print(f"Progress saved every {SAVE_INTERVAL} pairs\n")

        # Randomise order to prevent ordering effects on LLM judgements
        randomized = candidates.copy()
        random.shuffle(randomized)

        scored_relationships: List[Dict] = []
        valid_count = 0
        none_count = 0

        for idx, candidate in enumerate(randomized, 1):
            print(
                f"Scoring pair {idx}/{len(candidates)}: "
                f"{candidate['filepath1']} <-> {candidate['filepath2']}"
            )

            try:
                doc1_content = self.read_document(candidate["filepath1"])
                doc2_content = self.read_document(candidate["filepath2"])

                score_result = self.score_relationship(
                    doc1_content,
                    doc2_content,
                    candidate["filepath1"],
                    candidate["filepath2"],
                    candidate["shared_entities"],
                )

                rel_type = score_result["relationship_type"]
                if rel_type != "NONE":
                    valid_count += 1
                else:
                    none_count += 1

                # Carry through Phase 3 metadata so it's available in the graph
                scored_relationships.append(
                    {
                        "hash1": candidate["hash1"],
                        "hash2": candidate["hash2"],
                        "filepath1": candidate["filepath1"],
                        "filepath2": candidate["filepath2"],
                        "overlap_score": candidate["overlap_score"],
                        "semantic_score": candidate.get("semantic_score", 0.0),
                        "shared_entities": candidate["shared_entities"],
                        "gate": candidate.get("gate", "entity"),
                        "relationship": score_result,
                    }
                )

                print(
                    f"  {rel_type} | strength: {score_result['strength']}/10 | "
                    f"confidence: {score_result['confidence']} | "
                    f"running valid/NONE: {valid_count}/{none_count}"
                )

            except Exception as e:
                print(f"  Error processing pair: {str(e)}")
                continue

            # Incremental save — preserves progress if the run is interrupted
            if idx % SAVE_INTERVAL == 0:
                self.save_scored_relationships(scored_relationships)
                print(f"  [checkpoint] Saved {len(scored_relationships)} pairs so far")

        # Final save
        self.save_scored_relationships(scored_relationships)
        return scored_relationships

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_scored_relationships(self, relationships: List[Dict]):
        """Persist scored relationships to the staging file."""
        with open(self.staging_file, "w", encoding="utf-8") as f:
            json.dump(relationships, f, indent=2)

    def load_scored_relationships(self) -> List[Dict]:
        """Load previously saved scored relationships from staging."""
        if not self.staging_file.exists():
            return []
        with open(self.staging_file, "r", encoding="utf-8") as f:
            return json.load(f)


if __name__ == "__main__":
    from phase3_candidate_filtering import CandidateFilter

    filter_engine = CandidateFilter()
    candidates = filter_engine.load_candidates()

    if not candidates:
        print("No candidates found. Run phase3_candidate_filtering.py first.")
        exit(1)

    scorer = RelationshipScorer()
    scored_relationships = scorer.score_all_candidates(candidates)

    valid = [r for r in scored_relationships if r["relationship"]["relationship_type"] != "NONE"]

    print(f"\nPhase 4 complete:")
    print(f"  Total pairs scored:    {len(scored_relationships)}")
    print(f"  Valid relationships:   {len(valid)}")
    print(f"  Rejected (NONE):       {len(scored_relationships) - len(valid)}")
    print(f"  Saved to:              {scorer.staging_file}")
