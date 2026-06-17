"""Phase 3: Candidate pair filtering.

Reduces the O(n²) comparison space before the expensive LLM scoring in Phase 4.
A document pair becomes a candidate if it passes either of two gates:

  Primary gate   — entity overlap: shares at least MIN_ENTITY_OVERLAP canonical
                   entity strings (same as suhas-pipeline)

  Secondary gate — semantic fallback: TF-IDF cosine similarity on entity+topic
                   text exceeds MIN_SEMANTIC_SIMILARITY. Catches pairs where the
                   same concept appears under different names and normalization
                   in Phase 2 didn't unify them (e.g. "object storage pod" vs
                   "zen-minio" never sharing a canonical string).

Key design choices carried over from suhas-pipeline:
  - IDF-like entity specificity weighting (rare entities count more)
  - Weighted overlap score used for sorting (highest-confidence pairs first)
  - Candidates saved to staging so Phase 4 can be re-run without re-filtering

New in yann-pipeline:
  - TF-IDF semantic fallback gate (requires scikit-learn; degrades gracefully if absent)
  - Each candidate now records which gate it passed ('entity', 'semantic', or 'both')
    so you can inspect the distribution later
"""

import json
import math
from itertools import combinations
from typing import Any, Dict, List, Tuple

import config

# Semantic fallback requires scikit-learn — optional dependency
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as sk_cosine_similarity
    SEMANTIC_AVAILABLE = True
except ImportError:
    SEMANTIC_AVAILABLE = False
    print(
        "Warning: scikit-learn not found — semantic fallback gate disabled.\n"
        "Install it with:  pip install scikit-learn"
    )


class CandidateFilter:
    """Filter document pairs to identify candidates for LLM relationship scoring."""

    def __init__(self):
        self.staging_file = config.STAGING_DIR / "phase3_candidate_pairs.json"

    # ── Entity specificity & overlap ──────────────────────────────────────────

    def compute_entity_specificity(self, entity: str, canonical_index: Dict) -> float:
        """IDF-like score: entities that appear in fewer documents are more specific (0–1)."""
        if entity not in canonical_index:
            return 0.5  # conservative default for unknown entities

        total_docs = len(canonical_index)
        docs_with_entity = len(canonical_index[entity]["document_hashes"])

        if docs_with_entity == 0:
            return 0.0

        return 1.0 - (docs_with_entity / total_docs)

    def compute_overlap_score(
        self,
        entities1: List[str],
        entities2: List[str],
        canonical_index: Dict,
    ) -> Tuple[float, List[str]]:
        """Weighted intersection of two entity sets, normalised by average set size."""
        set1, set2 = set(entities1), set(entities2)
        shared = set1.intersection(set2)

        if not shared:
            return 0.0, []

        weighted_score = sum(
            self.compute_entity_specificity(e, canonical_index) for e in shared
        )

        avg_size = (len(set1) + len(set2)) / 2
        if avg_size > 0:
            weighted_score /= avg_size

        return weighted_score, list(shared)

    # ── TF-IDF semantic fallback ──────────────────────────────────────────────

    def build_tfidf_index(
        self, extractions: Dict[str, Any]
    ) -> Tuple:
        """Vectorise each document as its entity+topic strings for cosine similarity.

        Returns (tfidf_matrix, doc_hashes_in_matrix_order) or (None, []) if
        sklearn is unavailable.
        """
        if not SEMANTIC_AVAILABLE:
            return None, []

        doc_hashes = list(extractions.keys())

        # Represent each doc as a bag of its entity and topic strings
        doc_texts = []
        for h in doc_hashes:
            extraction = extractions[h]["extraction"]
            text = " ".join(extraction["entities"] + extraction["topics"])
            doc_texts.append(text)

        vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
        tfidf_matrix = vectorizer.fit_transform(doc_texts)

        return tfidf_matrix, doc_hashes

    def compute_semantic_similarity(self, tfidf_matrix, idx1: int, idx2: int) -> float:
        """Cosine similarity between two document TF-IDF vectors."""
        if tfidf_matrix is None:
            return 0.0
        return float(sk_cosine_similarity(tfidf_matrix[idx1], tfidf_matrix[idx2])[0][0])

    # ── Main filtering ────────────────────────────────────────────────────────

    def filter_candidate_pairs(
        self, extractions: Dict[str, Any], canonical_index: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Evaluate all document pairs and return those passing either gate.

        Primary gate:   shared entity count >= MIN_ENTITY_OVERLAP
        Secondary gate: TF-IDF cosine similarity >= MIN_SEMANTIC_SIMILARITY
        """
        doc_hashes = list(extractions.keys())
        total_pairs = len(doc_hashes) * (len(doc_hashes) - 1) // 2

        print(f"Evaluating {total_pairs} possible document pairs...")

        # Build TF-IDF index once for all pairs (only if sklearn available)
        tfidf_matrix, tfidf_hashes = self.build_tfidf_index(extractions)
        hash_to_idx = {h: i for i, h in enumerate(tfidf_hashes)}

        if SEMANTIC_AVAILABLE:
            print(
                f"Semantic fallback enabled "
                f"(threshold: {config.MIN_SEMANTIC_SIMILARITY})"
            )

        candidates = []

        for hash1, hash2 in combinations(doc_hashes, 2):
            entities1 = extractions[hash1]["extraction"]["entities"]
            entities2 = extractions[hash2]["extraction"]["entities"]

            overlap_score, shared_entities = self.compute_overlap_score(
                entities1, entities2, canonical_index
            )

            passes_entity_gate = len(shared_entities) >= config.MIN_ENTITY_OVERLAP

            # Semantic gate — only computed if entity gate failed (saves time)
            semantic_score = 0.0
            passes_semantic_gate = False
            if not passes_entity_gate and tfidf_matrix is not None:
                idx1 = hash_to_idx[hash1]
                idx2 = hash_to_idx[hash2]
                semantic_score = self.compute_semantic_similarity(
                    tfidf_matrix, idx1, idx2
                )
                passes_semantic_gate = semantic_score >= config.MIN_SEMANTIC_SIMILARITY

            if passes_entity_gate or passes_semantic_gate:
                # Record which gate(s) passed for later inspection
                if passes_entity_gate and passes_semantic_gate:
                    gate = "both"
                elif passes_entity_gate:
                    gate = "entity"
                else:
                    gate = "semantic"

                candidates.append(
                    {
                        "hash1": hash1,
                        "hash2": hash2,
                        "filepath1": extractions[hash1]["filepath"],
                        "filepath2": extractions[hash2]["filepath"],
                        "overlap_score": overlap_score,
                        "semantic_score": semantic_score,
                        "shared_entities": shared_entities,
                        "entity_count1": len(entities1),
                        "entity_count2": len(entities2),
                        "gate": gate,
                    }
                )

        # Sort by overlap score descending (entity-gate candidates first, then semantic)
        candidates.sort(key=lambda x: x["overlap_score"], reverse=True)

        entity_count = sum(1 for c in candidates if c["gate"] in ("entity", "both"))
        semantic_only = sum(1 for c in candidates if c["gate"] == "semantic")

        print(f"\nFound {len(candidates)} candidate pairs (from {total_pairs} total)")
        print(f"  Passed entity gate:    {entity_count}")
        print(f"  Semantic fallback only: {semantic_only}")
        if total_pairs > 0:
            print(f"  Reduction:             {100 * (1 - len(candidates) / total_pairs):.1f}%")

        self.save_candidates(candidates)
        return candidates

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_candidates(self, candidates: List[Dict[str, Any]]):
        """Save candidate pairs to staging file."""
        with open(self.staging_file, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2)
        print(f"Saved candidates to {self.staging_file}")

    def load_candidates(self) -> List[Dict[str, Any]]:
        """Load candidate pairs from staging file."""
        if not self.staging_file.exists():
            return []
        with open(self.staging_file, "r", encoding="utf-8") as f:
            return json.load(f)


if __name__ == "__main__":
    from phase1_extraction import DocumentExtractor
    from phase2_normalization import EntityNormalizer

    extractor = DocumentExtractor()
    extractions = extractor.load_extractions()

    normalizer = EntityNormalizer()
    entity_mapping = normalizer.load_normalization()

    if not extractions or not entity_mapping:
        print("Missing phase 1 or 2 results. Run previous phases first.")
        exit(1)

    normalized_extractions = normalizer.apply_normalization(extractions, entity_mapping)
    canonical_index = normalizer.build_canonical_index(extractions, entity_mapping)

    filter_engine = CandidateFilter()
    candidates = filter_engine.filter_candidate_pairs(normalized_extractions, canonical_index)

    print(f"\nPhase 3 complete: {len(candidates)} candidate pairs identified")
