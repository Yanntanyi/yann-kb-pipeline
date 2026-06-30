"""Phase 1: Independent document extraction.

Reads every .md file under DOCUMENTS_DIR, extracts a structured INCIDENT-DOMAIN
fingerprint from each one in isolation using the LLM, and saves to staging.

Key design choices carried over from suhas-pipeline:
  - Content-based SHA-256 hashing for deduplication (same content = same hash,
    so duplicate files under different names are processed only once)
  - Each document is processed independently — no cross-referencing — to prevent
    first-mover bias in entity vocabulary

Domain schema (replaces the generic entities/topics/stance/date schema):
  The corpus is RCAs (incidents) and CRs (changes), not generic documents, so we
  extract the fields the business and technical questions actually pivot on —
  doc_type, root_cause, resolution, status, customer_impact, components, teams,
  severity, tickets — alongside the entities/topics/date the graph already used.
  This is what lets the corpus-wide (thematic) path answer "contributing factors /
  resolution / who was involved / which are still open" from the node itself
  instead of from bare topic tags. The old generic 'stance' field is dropped.

  'date' (YYYY-MM-DD or null) still feeds Phase 5's PRECEDED_BY temporal edges;
  most of today's corpus is dateless, but new documents are expected to carry
  dates, so the temporal machinery stays.
"""

import json
import hashlib
from pathlib import Path
from typing import Any, Dict

from llm_client import get_llm_client
from date_utils import resolve_date
import config


class DocumentExtractor:
    """Extract semantic fingerprints from documents independently."""

    def __init__(self):
        self.llm = get_llm_client()
        self.staging_file = config.STAGING_DIR / "phase1_extractions.json"

    def compute_hash(self, content: str) -> str:
        """SHA-256 hash of raw document text, used as the document's unique ID."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Fields stored on each node. List fields default to [], scalars to None.
    LIST_FIELDS = ("entities", "topics", "components", "teams", "tickets")
    SCALAR_FIELDS = ("doc_type", "title", "root_cause", "resolution", "status",
                     "customer_impact", "severity")

    def extract_from_document(self, filepath: Path, content: str) -> Dict[str, Any]:
        """Ask the LLM to extract a structured incident-domain fingerprint."""
        prompt = f"""You are analyzing one document from an SRE incident knowledge base. The corpus is Root Cause Analyses (RCAs, i.e. incidents) and Change Requests (CRs, i.e. changes). Extract structured information and return ONLY valid JSON with no additional text.

Document content:
{content}

Return JSON with exactly these fields:
{{
  "doc_type": "one of: incident, change  (incident = an RCA / post-incident report; change = a change request / CR)",
  "title": "a concise human-readable title for this document",
  "entities": ["named entities as 'EntityName (Type)', where Type is one of: person, technology, concept, event, organization, location, other"],
  "topics": ["3-5 primary topics or themes"],
  "date": "the primary document date as YYYY-MM-DD, or null if none is stated",
  "components": ["specific systems/services/clusters/components affected or changed, e.g. 'Voice Gateway', 'cert-manager', 'MCOG', 'wa-incoming-webhooks'"],
  "teams": ["teams, organizations, or named people involved, e.g. 'IBM EM Team', 'Red Hat', 'UPS Voice'"],
  "root_cause": "for an incident: ONE sentence stating the underlying root cause. null for a change.",
  "resolution": "how the incident was resolved/remediated, or what the change accomplished. null if not stated.",
  "status": "one of: resolved, open, unknown — whether the incident is fully resolved; for a completed change use 'resolved'",
  "customer_impact": "for an incident: a short phrase capturing customer impact (calls affected, outage duration, channel Voice/Digital). null if none or not stated.",
  "severity": "the severity if stated (e.g. 'Sev1', 'Sev2'), else null",
  "tickets": ["any case/ticket/reference IDs mentioned, e.g. 'TS020583334', 'RedHat 04271299'"]
}}

Be specific and grounded in the document — do not invent values. Use null (or []) for anything the document does not state. Return ONLY the JSON object, no explanations."""

        try:
            result = self.llm.generate_json(prompt)

            # entities / topics / doc_type are required; the rest are optional.
            for field in ("entities", "topics", "doc_type"):
                if field not in result:
                    raise ValueError(f"Missing required field: {field}")

            result = self._normalize_fields(result)

            # Deterministic date overlay: filename-authoritative, keep a good LLM
            # date, else fall back to a body date. The LLM only sees the body and
            # often misses filename dates / dates-in-passing, so this adds coverage
            # without overwriting a date the LLM already got right.
            result["date"] = resolve_date(filepath.name, content, result.get("date"))

            return result

        except Exception as e:
            print(f"  Error extracting from {filepath.name}: {str(e)}")
            fallback = self._normalize_fields({"topics": ["unknown"], "doc_type": "unknown"})
            fallback["date"] = resolve_date(filepath.name, content, None)
            return fallback

    def _normalize_fields(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Guarantee every schema field exists with the right shape (list vs scalar),
        so downstream phases and the Neo4j node never hit a missing key."""
        for f in self.LIST_FIELDS:
            v = result.get(f)
            result[f] = v if isinstance(v, list) else ([] if v in (None, "") else [v])
        for f in self.SCALAR_FIELDS:
            v = result.get(f)
            result[f] = v if (v not in ("", "null", "none")) else None
        return result

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
