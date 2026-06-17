"""Pipeline configuration — paths, thresholds, connection settings."""

from pathlib import Path

# Root of the UPS-Watson-Knowledge-Base repo
BASE_DIR = Path(__file__).parent.parent

# Documents directory — contains CR/ and RCA/ subdirectories
DOCUMENTS_DIR = BASE_DIR / "docs"

# Only ingest from these subdirectories — everything else under docs/ is ignored
DOCUMENT_SUBDIRS = ["CR", "RCA"]

# Filenames to skip even when found inside an ingested subdirectory
SKIP_FILENAMES = {
    "README.md",
    "INDEX.md",
    "Template - Peak Period.md",
    "Template - UPS Change Request - Peak Period.md",
}

# Staging directory for intermediate JSON results between phases
STAGING_DIR = Path(__file__).parent / "staging"
STAGING_DIR.mkdir(exist_ok=True)

# ── Phase 3 ──────────────────────────────────────────────────────────────────
# Minimum number of shared canonical entities for a doc pair to become a candidate
MIN_ENTITY_OVERLAP = 2

# Minimum TF-IDF cosine similarity for the semantic fallback gate (0.0–1.0)
# Pairs that fail the entity overlap gate but exceed this threshold still become candidates
MIN_SEMANTIC_SIMILARITY = 0.25

# ── Phase 5 ──────────────────────────────────────────────────────────────────
# Relationships below this strength (1–10) are dropped before writing to Neo4j
MIN_RELATIONSHIP_STRENGTH = 4

# Maximum edges per document node — prevents one doc from dominating the graph
MAX_DEGREE_PER_NODE = 10

# ── LM Studio ────────────────────────────────────────────────────────────────
LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
# Must match the model name shown in LM Studio's loaded model list
LM_STUDIO_MODEL = "ibm/granite-4.1-8b"

# ── Neo4j ─────────────────────────────────────────────────────────────────────
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password1234"  # set this to your actual Neo4j password
