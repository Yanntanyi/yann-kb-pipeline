"""Pipeline configuration — paths, thresholds, connection settings."""

import os
from pathlib import Path

# Load .env file if present (never committed to git — see .gitignore).
# python-dotenv is an optional dev dependency; skip silently if not installed.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass


def _env(*names: str, default: str = "") -> str:
    """Return the first non-empty value among the given environment variables,
    or `default`. Lets a setting accept several env var names (e.g. both
    WATSONX_* and REPORT_WATSONX_*)."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


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

# Number of candidate pairs scored concurrently in Phase 4. Each pair is one
# LLM call; scoring them in parallel turns a many-hour sequential run into a
# fraction of the time. Lower this if the LLM endpoint returns rate-limit (429)
# errors faster than the client's backoff can absorb.
PHASE4_CONCURRENCY = int(_env("PHASE4_CONCURRENCY", default="8"))

# ── LLM provider ───────────────────────────────────────────────────────────────
# Which backend the whole pipeline uses: "watsonx" (gpt-oss via watsonx.ai)
# or "lmstudio" (local LM Studio). get_llm_client() reads this.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "watsonx")

# ── LM Studio ────────────────────────────────────────────────────────────────
LM_STUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
# Must match the model name shown in LM Studio's loaded model list
LM_STUDIO_MODEL = "ibm/granite-4.1-8b"

# ── watsonx.ai ─────────────────────────────────────────────────────────────────
# Values are read from the environment first, falling back to defaults below.
# Both WATSONX_* and REPORT_WATSONX_* env var names are accepted.
# Region host — NOT the IAM host. The chat endpoint is appended by the client.
WATSONX_BASE_URL = _env(
    "WATSONX_BASE_URL", "REPORT_WATSONX_BASE_URL",
    default="https://us-south.ml.cloud.ibm.com",
)
WATSONX_API_KEY = _env("WATSONX_API_KEY", "REPORT_WATSONX_API_KEY")
WATSONX_PROJECT_ID = _env("WATSONX_PROJECT_ID", "REPORT_WATSONX_PROJECT_ID")
WATSONX_MODEL = _env("WATSONX_MODEL", default="openai/gpt-oss-120b")
# API version date for the /ml/v1/text/chat endpoint (YYYY-MM-DD).
WATSONX_API_VERSION = _env("WATSONX_API_VERSION", default="2024-05-31")

# ── Embeddings ─────────────────────────────────────────────────────────────────
# Used for the dense half of hybrid seed retrieval. EMBEDDING_DIM MUST match the
# model's output dimensionality (granite-embedding-278m & slate-125m = 768,
# multilingual-e5-large = 1024) — it sets the Elasticsearch dense_vector mapping.
WATSONX_EMBED_MODEL = _env(
    "WATSONX_EMBED_MODEL", default="ibm/granite-embedding-278m-multilingual"
)
WATSONX_EMBED_BATCH = int(_env("WATSONX_EMBED_BATCH", default="100"))
EMBEDDING_DIM = int(_env("EMBEDDING_DIM", default="768"))
# Embedding model id when LLM_PROVIDER=lmstudio (must be loaded in LM Studio).
LM_STUDIO_EMBED_MODEL = _env(
    "LM_STUDIO_EMBED_MODEL", default="text-embedding-granite-embedding-278m-multilingual"
)

# ── Elasticsearch (seed retrieval) ───────────────────────────────────────────────
ES_ENABLED = _env("ES_ENABLED", default="true").lower() in ("1", "true", "yes")
ES_URL = _env("ES_URL", default="http://localhost:9200")
ES_INDEX = _env("ES_INDEX", default="ups-watson-docs")
# Optional basic auth, e.g. "elastic:changeme". Empty = no auth (local container).
ES_BASIC_AUTH = _env("ES_BASIC_AUTH")
# Hybrid retrieval: BM25 + dense kNN fused with RRF. Set false for BM25-only
# (no embedding dependency at index or query time).
ES_USE_DENSE = _env("ES_USE_DENSE", default="true").lower() in ("1", "true", "yes")
# RRF constant — higher = flatter fusion, less weight on top ranks.
ES_RRF_K = int(_env("ES_RRF_K", default="60"))

# ── Seed selection ───────────────────────────────────────────────────────────────
# Number of top retrieved documents used as traversal starting points. Multi-seed
# makes the system robust to a single wrong seed. Must be < MAX_DOCS in ask.py.
NUM_SEEDS = int(_env("NUM_SEEDS", default="3"))

# ── Neo4j ─────────────────────────────────────────────────────────────────────
# Env-overridable (via .env or shell) so the connection can be pointed at a
# different host/credentials without editing this file.
NEO4J_URI = _env("NEO4J_URI", default="bolt://localhost:7687")
NEO4J_USER = _env("NEO4J_USER", default="neo4j")
NEO4J_PASSWORD = _env("NEO4J_PASSWORD", default="password123")

# ── Shared answer-generation instructions ────────────────────────────────────
# Used by BOTH the graph system and the flat-RAG baseline so the head-to-head
# comparison measures RETRIEVAL, not answer wording. (Previously the graph prompt
# capped length — "a few tight bullet points; do NOT pad" — while the baseline did
# not, which confounded the completeness metric: the graph was penalized for being
# told to be terse, not for missing content.)
ANSWER_INSTRUCTIONS = """Instructions:
- Lead with the direct answer in the first sentence. No preamble, no restating the question.
- Cover all the specific facts the question asks for — if it asks to trace a chain, give every step; if it asks for several items, include them all. Be complete, but do not pad with background the question didn't ask for.
- Documents may carry structured or labeled fields — e.g. Risk, Severity, Priority, Status, Category, Type, Date, Component, Team — as headers, key/value lines, or front-matter, not only as prose. Read these fields as authoritative facts. Before concluding a detail (like a risk level, status, category, or date) is missing, check the labeled fields and headers; do not say the information is unavailable if it appears there.
- Cite specific document names, dates, and components where they matter.
- You may use light Markdown (**bold**, "- " bullets); it will be rendered.
- If the documents don't fully answer the question, say so. Do not introduce outside information.
- Output only the final answer, not your reasoning."""
