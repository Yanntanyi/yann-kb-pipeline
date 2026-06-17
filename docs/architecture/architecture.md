# UPS Watson Incident Intelligence: Architecture

---

## The Problem

Every time there's a cloud infrastructure incident at UPS, engineers write an RCA (Root Cause Analysis). Every controlled change goes through a Change Request. Over time you accumulate hundreds of these documents — but they just sit in folders.

When a new incident hits, nobody knows if this exact problem happened six months ago, which services were involved, who approved the last change to that component, or whether a recent CR is what triggered the outage. You're starting from zero every single time.

---

## Why Not Just RAG

The obvious first answer is RAG — dump the documents into a search engine and ask questions. The problem is that standard RAG only finds documents that match your words. It doesn't understand that zen-minio and the memory alerts and the IBM EM team are all part of the same story. It understands words, not relationships.

A concrete example: the CPD certificate RCA and the zen-minio memory CR both mention IBM EM Team and OpenShift. A keyword search finds both when you search "IBM EM." But it has no idea that one is a change request that was executed and the other is a post-incident report written the same day because that change caused an outage. The relationship — and the causal direction — is invisible.

What we need is a system that understands both the content of documents and the connections between them.

---

## What We Built

A **5-phase pipeline** that reads and understands every document, then constructs a **knowledge graph** in Neo4j with two types of nodes:

| Node type | What it represents | Example |
|-----------|-------------------|---------|
| `Document` | A single CR or RCA file | `CR/20250213 - Zenminio.md` |
| `Entity` | Any named thing extracted from the docs — a service, team, cluster, person, or concept | `zen-minio (Technology)`, `IBM EM Team (Organization)` |

And **9 edge types** across three categories:

**Semantic edges (Document → Document)** — scored by the LLM in Phase 4:

| Edge | Meaning |
|------|---------|
| `EXTENDS` | One document builds upon or extends concepts from the other |
| `CONTRADICTS` | The documents present conflicting information |
| `SUPPORTS` | One document provides evidence or support for the other |
| `REFERENCES` | One document explicitly cites or links to the other |
| `PROVIDES_CONTEXT_FOR` | One document gives background needed to understand the other |
| `SHARES_DOMAIN_WITH` | Same domain, but no direct relationship |
| `IMPLEMENTS` | One document implements ideas or decisions from the other |

**Structural edge (Document → Entity):**

| Edge | Meaning |
|------|---------|
| `MENTIONS` | This document references this entity |

**Temporal edge (Document → Document):**

| Edge | Meaning |
|------|---------|
| `PRECEDED_BY` | This document is dated earlier than the other |

The result: when an engineer asks *"what changed before the November cert outage?"*, the system doesn't keyword-search — it traverses from the incident RCA, follows `PRECEDED_BY` edges backward, finds the cert CR from the same week, and returns it with its full context and approval chain.

---

## The 5-Phase Pipeline

Every phase saves its output to `graph-pipeline/staging/` as a JSON file. This means you can resume from any phase without re-running the expensive ones before it.

---

### Phase 1 — Independent Extraction (`phase1_extraction.py`)

Reads every `.md` file under `docs/` and asks the LLM to extract a structured fingerprint from each document **in isolation** — no document sees another yet.

Each document produces:
```json
{
  "entities": ["zen-minio (Technology)", "IBM EM Team (Organization)", ...],
  "topics":   ["memory management", "openshift configuration"],
  "stance":   "instructional",
  "date":     "2025-02-13"
}
```

**Why isolation?** If documents were processed together, the first documents processed would anchor the vocabulary. Every entity name extracted later would be implicitly compared against those early examples. By processing each document independently and normalizing in Phase 2, we prevent any document from having a disproportionate influence on how entities are named.

**Why date?** Every CR and RCA has a date at the top. Extracting it here gives Phase 5 the data it needs to create temporal edges — which CRs preceded which incidents.

**Why content-based hashing?** Documents are deduplicated by SHA-256 hash of their content, not by filename. If the same document gets copied under a different name, it's processed only once.

Saves to: `staging/phase1_extractions.json`

---

### Phase 2 — Global Entity Normalization (`phase2_normalization.py`)

Collects every entity string from every document into one pool and sends them to the LLM in batches of 100 to resolve aliases into canonical forms.

```
"IBM EM"        →  "IBM EM Team (Organization)"
"the EM team"   →  "IBM EM Team (Organization)"
"EM Team"       →  "IBM EM Team (Organization)"
```

It also builds a **canonical index** — a map of every canonical entity to the list of documents that mention it:

```json
{
  "IBM EM Team (Organization)": {
    "document_hashes": ["abc123", "def456", "ghi789"],
    "mention_count": 7
  }
}
```

This canonical index is what Phase 3 uses for overlap scoring, and what Phase 5 uses to create Entity nodes and MENTIONS edges in Neo4j.

**Why global normalization?** If each document normalized its own entities independently, "IBM EM Team" and "EM Team" would never be recognized as the same thing across two documents. Normalization only works if it sees all entity strings at once.

Saves to: `staging/phase2_normalized_entities.json`

---

### Phase 3 — Candidate Filtering (`phase3_candidate_filtering.py`)

With ~294 documents, a naive all-pairs comparison would mean ~43,000 LLM calls in Phase 4. Phase 3 reduces this to a manageable candidate list using two gates:

**Primary gate — entity overlap:**
A document pair passes if they share at least `MIN_ENTITY_OVERLAP` canonical entities. Shared entities are weighted by specificity — an entity that appears in only 2 documents out of 294 is a much stronger signal than one that appears in 100 documents.

**Secondary gate — semantic similarity (TF-IDF fallback):**
If a pair fails the entity gate but scores above `MIN_SEMANTIC_SIMILARITY` on TF-IDF cosine similarity of their entity+topic text, it still becomes a candidate. This catches cases where two documents describe the same thing using different terminology that Phase 2 normalization didn't unify — for example, "object storage service" vs "zen-minio" might not share any canonical entity strings but would score high on term overlap.

Each candidate records which gate it passed (`"entity"`, `"semantic"`, or `"both"`), so you can inspect the distribution later.

Saves to: `staging/phase3_candidate_pairs.json`

---

### Phase 4 — Pairwise LLM Scoring (`phase4_relationship_scoring.py`)

For each candidate pair, reads both full documents and asks the LLM to score their relationship:

```json
{
  "relationship_type": "PROVIDES_CONTEXT_FOR",
  "strength":          8,
  "description":       "The CR documents the cert update that caused the voice outage described in the RCA",
  "directionality":    "doc1_to_doc2",
  "confidence":        "high"
}
```

Supported relationship types:

| Type | Meaning |
|------|---------|
| `EXTENDS` | One document builds on the other |
| `CONTRADICTS` | Documents conflict |
| `SUPPORTS` | One provides evidence for the other |
| `REFERENCES` | One explicitly cites the other |
| `PROVIDES_CONTEXT_FOR` | One gives background to understand the other |
| `SHARES_DOMAIN_WITH` | Same domain, no direct relationship |
| `IMPLEMENTS` | One implements ideas from the other |
| `NONE` | No meaningful relationship — rejected |

**Why full documents?** An RCA tells a story — splitting it into chunks risks cutting the cause from the resolution. The LLM needs to see the whole narrative to correctly judge whether two documents are causally related.

**Why randomised order?** LLMs can exhibit ordering effects — the first pair scored can subtly influence later scores if there's any shared context. Randomising the order before scoring prevents this.

**Why incremental saving?** Phase 4 can run for hours on a large corpus. Results are checkpointed every 10 pairs so a crash or LLM timeout doesn't lose everything.

Saves to: `staging/phase4_scored_relationships.json`

---

### Phase 5 — Graph Construction (`phase5_graph_construction.py`)

Takes everything from previous phases and writes the final graph to Neo4j in five steps:

**Step 1 — Filter by quality thresholds:**
Drops any relationship scored `NONE`, below `MIN_RELATIONSHIP_STRENGTH`, or with `low` confidence. Only meaningful, high-confidence relationships reach the graph.

**Step 2 — Apply degree cap:**
For each document, keeps only its top `MAX_DEGREE_PER_NODE` strongest connections. Prevents any one document from becoming an over-connected hub that dominates graph traversal.

**Step 3 — Create Document nodes:**
One node per unique document, storing filepath, entities, topics, stance, and date.

**Step 4 — Create Entity nodes + MENTIONS edges:**
This is the key structural improvement over a document-only graph. Every canonical entity from Phase 2 becomes a real Neo4j node. Every document gets a `MENTIONS` edge to each entity it references.

This makes queries like *"find all documents involving zen-minio"* a graph traversal instead of a full-text scan:
```cypher
MATCH (e:Entity {name: "zen-minio"})<-[:MENTIONS]-(d:Document)
RETURN d.filepath
```

Without entity nodes, that query would require opening every Document node and searching its `entities` list property — defeating the purpose of having a graph.

**Step 5 — Create PRECEDED_BY temporal edges:**
For each related document pair where both documents have a date, creates a directed `PRECEDED_BY` edge from the earlier document to the later one, with `days_apart` stored on the edge.

Only created between documents that already share a relationship edge — we don't temporally connect every older document to every newer one, just the pairs the graph already says are related.

This enables the most important incident management query: *"what changes happened in the 7 days before this outage?"*
```cypher
MATCH (rca:Document {filepath: "RCA/CPD_Cert_Issue.md"})
MATCH (cr:Document)-[t:PRECEDED_BY]->(rca)
WHERE t.days_apart <= 7
RETURN cr.filepath, t.days_apart
ORDER BY t.days_apart
```

---

## Prerequisites

### 1. Neo4j (local instance)

Download Neo4j Desktop from [neo4j.com/download](https://neo4j.com/download/), create a local database, and start it.

Then open `graph-pipeline/config.py` and set:
```python
NEO4J_URI      = "bolt://localhost:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "your-actual-password"
```

### 2. LM Studio

Download from [lmstudio.ai](https://lmstudio.ai/), load a model (we use Granite 4.1 8B Instruct), and start the local server on port 1234.

Then set the model name in `graph-pipeline/config.py`:
```python
LM_STUDIO_MODEL = "granite-4.1-8b-instruct"
```
The model name must exactly match what LM Studio shows as the loaded model identifier.

### 3. Python dependencies

```bash
cd graph-pipeline
pip install neo4j scikit-learn requests
```

---

## Running the Pipeline

**Full run from scratch:**
```bash
cd graph-pipeline
python3 main.py
```

**Resume from a specific phase** (useful when phases 1-2 already ran and you want to re-run from candidate filtering with different settings):
```bash
python3 main.py --from-phase 3
```

**Re-run only graph construction** (e.g. you tuned a threshold in `config.py`):
```bash
python3 main.py --from-phase 5
```

**Run a single phase manually:**
```bash
python3 phase1_extraction.py
python3 phase2_normalization.py
python3 phase3_candidate_filtering.py
python3 phase4_relationship_scoring.py
python3 phase5_graph_construction.py
```

Each phase reads its inputs from `staging/` and writes its outputs back to `staging/`, so they can be run individually or chained.

---

## Tunable Parameters

All thresholds live in `graph-pipeline/config.py`:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `MIN_ENTITY_OVERLAP` | `2` | Min shared entities for Phase 3 primary gate |
| `MIN_SEMANTIC_SIMILARITY` | `0.25` | Min TF-IDF cosine similarity for Phase 3 fallback gate |
| `MIN_RELATIONSHIP_STRENGTH` | `4` | Min LLM strength score (1–10) for Phase 5 to write an edge |
| `MAX_DEGREE_PER_NODE` | `10` | Max edges per document node |

If the graph is too sparse, lower `MIN_RELATIONSHIP_STRENGTH` or `MIN_ENTITY_OVERLAP`. If it's too dense (hub documents dominating), lower `MAX_DEGREE_PER_NODE` or raise `MIN_RELATIONSHIP_STRENGTH`. After any threshold change, re-run from Phase 5 only — no need to re-run the LLM phases.

---

*Built for UPS Watson Incident Intelligence — June 2026*
