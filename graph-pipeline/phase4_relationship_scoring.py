"""Phase 4: Pairwise LLM relationship scoring.

For each candidate pair produced by Phase 3, reads both full documents and asks
the LLM to determine the type, strength, direction, and confidence of the
relationship between them.

Key design choices:
  - Randomised processing order to prevent ordering bias in LLM judgements
  - Full document content passed to the LLM (not chunks) so it sees the whole story
  - NONE relationship type used as a conservative rejection — Phase 5 drops these
  - Descriptions written as directional traversal framing, not generic summaries:
    the description answers "if I just read the source doc and follow this edge,
    what will I find in the destination doc and why am I going there?" This is
    what gets prepended to a document when the traversal system retrieves it.

Resume support:
  - Checkpoints every SAVE_INTERVAL pairs to staging
  - On restart, loads existing checkpoint and skips already-scored pairs —
    --from-phase 4 picks up exactly where the run left off
"""

import json
import random
from typing import Dict, List

from llm_client import LMStudioClient
import config

SAVE_INTERVAL = 10


class RelationshipScorer:
    """Score relationships between candidate document pairs using the LLM."""

    def __init__(self):
        self.llm = LMStudioClient()
        self.staging_file = config.STAGING_DIR / "phase4_scored_relationships.json"

    # ── Document reading ──────────────────────────────────────────────────────

    def read_document(self, filepath: str) -> str:
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
        """Ask the LLM to evaluate the relationship between two documents.

        The description field is the most important output for the traversal
        system. It must be written as directional framing: a sentence that tells
        a reader who just finished Document 1 exactly what Document 2 contains
        and why following this edge is worth doing.
        """
        entities_str = ", ".join(shared_entities) if shared_entities else "none identified"

        prompt = f"""You are analyzing the relationship between two incident management documents.
They share these entities: {entities_str}

DOCUMENT 1 ({filepath1}):
{doc1_content}

---

DOCUMENT 2 ({filepath2}):
{doc2_content}

---

Evaluate their relationship and return ONLY valid JSON:
{{
  "relationship_type": "one of: EXTENDS, CONTRADICTS, SUPPORTS, REFERENCES, PROVIDES_CONTEXT_FOR, SHARES_DOMAIN_WITH, IMPLEMENTS, NONE",
  "strength": <integer 1-10, where 10 is strongest>,
  "directionality": "one of: symmetric, doc1_to_doc2, doc2_to_doc1",
  "confidence": "one of: high, medium, low",
  "description": "see instructions below"
}}

Relationship type definitions:
- EXTENDS: One document builds upon or extends concepts from the other
- CONTRADICTS: Documents present conflicting information or approaches
- SUPPORTS: One document corroborates or provides evidence for the other
- REFERENCES: One document explicitly cites or links to the other
- PROVIDES_CONTEXT_FOR: One document gives the background needed to understand the other
- SHARES_DOMAIN_WITH: Same domain or technology area, but no direct relationship
- IMPLEMENTS: One document is the action taken as a result of the other
- NONE: No meaningful relationship — use this if the connection is weak or incidental

For the description field:
Write one sentence from the perspective of someone who has just finished reading
the source document and is deciding whether to read the destination document.
The sentence must answer: what does the destination document contain, and why is
it directly relevant to what was just read? Be specific — name the actual
components, incidents, or decisions involved. Do not write a generic summary.

Good example: "Document 2 is the change request that updated the CPD certificate
routes one day before the voice outage described in Document 1, containing the
specific implementation steps and approval chain for that change."

Bad example: "The two documents are related because they both discuss certificates."

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
        """Score every candidate pair, resuming from checkpoint if one exists."""
        if not candidates:
            print("No candidates to score")
            return []

        # Load any existing checkpoint so we can skip already-scored pairs
        already_scored = self.load_scored_relationships()
        scored_pairs = {
            frozenset([r["hash1"], r["hash2"]]) for r in already_scored
        }
        scored_relationships = list(already_scored)
        valid_count = sum(
            1 for r in already_scored
            if r["relationship"]["relationship_type"] != "NONE"
        )
        none_count = len(already_scored) - valid_count

        # Filter candidates down to only unscored pairs
        remaining = [
            c for c in candidates
            if frozenset([c["hash1"], c["hash2"]]) not in scored_pairs
        ]

        if already_scored:
            print(
                f"Resuming: {len(already_scored)} already scored, "
                f"{len(remaining)} remaining of {len(candidates)} total"
            )
        else:
            print(f"Scoring {len(candidates)} candidate pairs...")

        print(f"Progress saved every {SAVE_INTERVAL} pairs\n")

        random.shuffle(remaining)

        for idx, candidate in enumerate(remaining, 1):
            global_idx = len(already_scored) + idx
            print(
                f"Scoring pair {global_idx}/{len(candidates)}: "
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

            if idx % SAVE_INTERVAL == 0:
                self.save_scored_relationships(scored_relationships)
                print(f"  [checkpoint] Saved {len(scored_relationships)} pairs so far")

        self.save_scored_relationships(scored_relationships)
        return scored_relationships

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_scored_relationships(self, relationships: List[Dict]):
        with open(self.staging_file, "w", encoding="utf-8") as f:
            json.dump(relationships, f, indent=2)

    def load_scored_relationships(self) -> List[Dict]:
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

    valid = [
        r for r in scored_relationships
        if r["relationship"]["relationship_type"] != "NONE"
    ]

    print(f"\nPhase 4 complete:")
    print(f"  Total pairs scored:    {len(scored_relationships)}")
    print(f"  Valid relationships:   {len(valid)}")
    print(f"  Rejected (NONE):       {len(scored_relationships) - len(valid)}")
    print(f"  Saved to:              {scorer.staging_file}")
