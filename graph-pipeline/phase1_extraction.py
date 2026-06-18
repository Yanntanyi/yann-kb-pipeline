"""Phase 1: Independent document extraction.

Reads every .md file under DOCUMENTS_DIR, extracts entities/topics/stance/date
from each one in isolation using the LLM, and saves the results to staging.

Key design choices carried over from suhas-pipeline:
  - Content-based SHA-256 hashing for deduplication (same content = same hash,
    so duplicate files under different names are processed only once)
  - Each document is processed independently — no cross-referencing — to prevent
    first-mover bias in entity vocabulary

New in yann-pipeline:
  - 'date' field added to extraction (YYYY-MM-DD or null)
    This feeds Phase 5's temporal edge construction (PRECEDED_BY edges)
"""

import json
import hashlib
from pathlib import Path
from typing import Any, Dict

from llm_client import get_llm_client
import config


class DocumentExtractor:
    """Extract semantic fingerprints from documents independently."""

    def __init__(self):
        self.llm = get_llm_client()
        self.staging_file = config.STAGING_DIR / "phase1_extractions.json"

    def compute_hash(self, content: str) -> str:
        """SHA-256 hash of raw document text, used as the document's unique ID."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def extract_from_document(self, filepath: Path, content: str) -> Dict[str, Any]:
        """Ask the LLM to extract a structured fingerprint from one document."""
        prompt = f"""Analyze the following document and extract structured information. Return ONLY valid JSON with no additional text.

Document content:
{content}

Extract the following and return as JSON:
{{
  "entities": ["list of named entities with their types, format as 'EntityName (Type)' where Type is one of: person, technology, concept, event, organization, location, other"],
  "topics": ["list of 3-5 primary topics or themes"],
  "stance": "one of: descriptive, argumentative, instructional, analytical, narrative",
  "date": "the document date in YYYY-MM-DD format, or null if no date is found"
}}

Return ONLY the JSON object, no explanations."""

        try:
            result = self.llm.generate_json(prompt)

            # entities / topics / stance are required; date is optional
            required_fields = ["entities", "topics", "stance"]
            for field in required_fields:
                if field not in result:
                    raise ValueError(f"Missing required field: {field}")

            # Guard: some models omit optional keys entirely
            if "date" not in result:
                result["date"] = None

            return result

        except Exception as e:
            print(f"  Error extracting from {filepath.name}: {str(e)}")
            return {
                "entities": [],
                "topics": ["unknown"],
                "stance": "descriptive",
                "date": None,
            }

    def process_all_documents(self) -> Dict[str, Dict[str, Any]]:
        """Process all .md files under DOCUMENTS_DIR (recursive — finds CR/ and RCA/)."""
        extractions: Dict[str, Dict[str, Any]] = {}

        doc_files = sorted(
            f
            for subdir in config.DOCUMENT_SUBDIRS
            for f in (config.DOCUMENTS_DIR / subdir).glob("*.md")
            if f.name not in config.SKIP_FILENAMES
        )

        if not doc_files:
            print(f"No markdown files found under {config.DOCUMENTS_DIR}")
            return extractions

        print(f"Found {len(doc_files)} documents to process")

        for idx, filepath in enumerate(doc_files, 1):
            print(f"Processing {idx}/{len(doc_files)}: {filepath.name}")

            try:
                content = filepath.read_text(encoding="utf-8")
                doc_hash = self.compute_hash(content)

                if doc_hash in extractions:
                    print(f"  Skipping duplicate: {filepath.name}")
                    continue

                extraction = self.extract_from_document(filepath, content)

                extractions[doc_hash] = {
                    "filepath": str(filepath.relative_to(config.DOCUMENTS_DIR)),
                    "filename": filepath.name,
                    "extraction": extraction,
                }

                date_str = extraction.get("date") or "no date found"
                print(
                    f"  Extracted {len(extraction['entities'])} entities, "
                    f"{len(extraction['topics'])} topics, date: {date_str}"
                )

            except Exception as e:
                print(f"  Error processing {filepath.name}: {str(e)}")
                continue

        self.save_extractions(extractions)
        return extractions

    def save_extractions(self, extractions: Dict[str, Dict[str, Any]]):
        """Persist extraction results to the staging file."""
        with open(self.staging_file, "w", encoding="utf-8") as f:
            json.dump(extractions, f, indent=2)
        print(f"\nSaved {len(extractions)} extractions to {self.staging_file}")

    def load_extractions(self) -> Dict[str, Dict[str, Any]]:
        """Load previously saved extractions from staging (used by later phases)."""
        if not self.staging_file.exists():
            return {}
        with open(self.staging_file, "r", encoding="utf-8") as f:
            return json.load(f)


if __name__ == "__main__":
    extractor = DocumentExtractor()
    extractions = extractor.process_all_documents()
    print(f"\nPhase 1 complete: {len(extractions)} unique documents processed")
