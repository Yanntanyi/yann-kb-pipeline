"""Phase 2: Global entity normalization.

Collects every entity string from every document (from Phase 1), sends them to
the LLM in batches to resolve aliases into canonical forms, then builds a
canonical index that maps each entity → the list of documents that mention it.

Key design choices carried over from suhas-pipeline:
  - All entities are collected and normalized together (global, not per-doc)
    to prevent first-processed documents from dominating the vocabulary
  - Batch size of 100 keeps prompts within context limits while still
    catching aliases that appear across documents

Fixed in yann-pipeline:
  - apply_normalization used doc_data.copy() which is a shallow copy — writing
    canonical_entities back then mutated the original extractions dict in memory.
    Fixed with copy.deepcopy().
"""

import copy
import json
from collections import defaultdict
from typing import Dict, List

from llm_client import LMStudioClient
import config


class EntityNormalizer:
    """Normalize entities across all documents to prevent first-mover bias."""

    def __init__(self):
        self.llm = LMStudioClient()
        self.staging_file = config.STAGING_DIR / "phase2_normalized_entities.json"

    # ── Collection ────────────────────────────────────────────────────────────

    def collect_all_entities(self, extractions: Dict[str, Dict]) -> List[str]:
        """Pool every entity string from every document into one flat list."""
        all_entities = []
        for doc_data in extractions.values():
            all_entities.extend(doc_data["extraction"]["entities"])
        return all_entities

    # ── Normalization ─────────────────────────────────────────────────────────

    def normalize_entities_batch(self, entities: List[str]) -> Dict[str, str]:
        """Ask the LLM to resolve a batch of raw entity strings to canonical forms."""
        unique_entities = list(dict.fromkeys(entities))  # dedupe, preserve order

        if not unique_entities:
            return {}

        prompt = f"""You are normalizing entity names to create canonical forms. Entities that refer to the same thing should map to the same canonical name.

Entity List:
{json.dumps(unique_entities, indent=2)}

Rules:
1. Group entities that refer to the same thing (e.g., "GPT-4", "GPT4", "OpenAI's GPT-4" -> "GPT-4")
2. Use the most common or standard form as canonical
3. Preserve entity types in parentheses
4. Keep distinct entities separate

Return ONLY valid JSON mapping each entity to its canonical form:
{{
  "raw_entity_1": "canonical_form_1",
  "raw_entity_2": "canonical_form_1",
  "raw_entity_3": "canonical_form_3"
}}

Return ONLY the JSON object, no explanations."""

        try:
            return self.llm.generate_json(prompt)
        except Exception as e:
            print(f"  Error normalizing entities: {str(e)}")
            # Fallback: identity mapping so the pipeline can continue
            return {entity: entity for entity in unique_entities}

    def normalize_all_entities(self, extractions: Dict[str, Dict]) -> Dict[str, str]:
        """Normalize all entities across all documents, processing in batches."""
        print("Collecting all entities...")
        all_entities = self.collect_all_entities(extractions)
        print(f"Found {len(all_entities)} total entity mentions")

        batch_size = 100
        entity_mapping: Dict[str, str] = {}

        for i in range(0, len(all_entities), batch_size):
            batch = all_entities[i : i + batch_size]
            print(f"Normalizing batch {i // batch_size + 1} ({len(batch)} entities)...")
            batch_mapping = self.normalize_entities_batch(batch)
            entity_mapping.update(batch_mapping)

        self.save_normalization(entity_mapping)
        return entity_mapping

    # ── Canonical index ───────────────────────────────────────────────────────

    def build_canonical_index(
        self, extractions: Dict[str, Dict], entity_mapping: Dict[str, str]
    ) -> Dict[str, Dict]:
        """Build a map of canonical entity → {name, document_hashes, mention_count}.

        This index is what Phase 3 uses for weighted overlap scoring, and what
        Phase 5 uses to create Entity nodes and MENTIONS edges in Neo4j.
        """
        canonical_index: Dict[str, Dict] = defaultdict(
            lambda: {"canonical_name": "", "document_hashes": [], "mention_count": 0}
        )

        for doc_hash, doc_data in extractions.items():
            for raw_entity in doc_data["extraction"]["entities"]:
                canonical = entity_mapping.get(raw_entity, raw_entity)

                canonical_index[canonical]["canonical_name"] = canonical
                if doc_hash not in canonical_index[canonical]["document_hashes"]:
                    canonical_index[canonical]["document_hashes"].append(doc_hash)
                canonical_index[canonical]["mention_count"] += 1

        return dict(canonical_index)

    # ── Apply normalization ───────────────────────────────────────────────────

    def apply_normalization(
        self, extractions: Dict[str, Dict], entity_mapping: Dict[str, str]
    ) -> Dict[str, Dict]:
        """Return a new extractions dict with raw entity strings replaced by canonical ones.

        Uses deepcopy so the original extractions dict is never mutated — the
        shallow copy in the original code caused silent in-place modification.
        """
        normalized_extractions: Dict[str, Dict] = {}

        for doc_hash, doc_data in extractions.items():
            normalized_data = copy.deepcopy(doc_data)

            canonical_entities = list(
                set(
                    entity_mapping.get(entity, entity)
                    for entity in doc_data["extraction"]["entities"]
                )
            )
            normalized_data["extraction"]["entities"] = canonical_entities
            normalized_extractions[doc_hash] = normalized_data

        return normalized_extractions

    # ── Persistence ───────────────────────────────────────────────────────────

    def save_normalization(self, entity_mapping: Dict[str, str]):
        """Save the raw→canonical mapping to staging."""
        with open(self.staging_file, "w", encoding="utf-8") as f:
            json.dump(entity_mapping, f, indent=2)
        print(f"Saved entity normalization to {self.staging_file}")

    def load_normalization(self) -> Dict[str, str]:
        """Load a previously saved raw→canonical mapping from staging."""
        if not self.staging_file.exists():
            return {}
        with open(self.staging_file, "r", encoding="utf-8") as f:
            return json.load(f)


if __name__ == "__main__":
    from phase1_extraction import DocumentExtractor

    extractor = DocumentExtractor()
    extractions = extractor.load_extractions()

    if not extractions:
        print("No extractions found. Run phase1_extraction.py first.")
        exit(1)

    normalizer = EntityNormalizer()
    entity_mapping = normalizer.normalize_all_entities(extractions)
    canonical_index = normalizer.build_canonical_index(extractions, entity_mapping)

    print(f"\nPhase 2 complete:")
    print(f"  Total unique canonical entities: {len(canonical_index)}")
    print(f"  Total entity mappings: {len(entity_mapping)}")
