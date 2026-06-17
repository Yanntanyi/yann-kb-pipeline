# UPS Watson Incident Intelligence: Architecture

---

## The Problem

Every time there's a cloud infrastructure incident at UPS, engineers write an RCA (Root Cause Analysis). Every controlled change goes through a Change Request. Over time you accumulate hundreds of these documents — but they just sit in folders.

When a new incident hits, nobody knows if this exact problem happened six months ago, which services were involved, who approved the last change to that component, or whether a recent CR is what triggered the outage. You're starting from zero every single time.

---

## Why Not Just RAG

The obvious first answer is RAG — dump the documents into a search engine and ask questions. The problem is that standard RAG only finds documents that match your words. It doesn't understand that zen-minio and the memory alerts and the IBM EM team are all part of the same story. It understands words, not relationships.

A concrete example: the CPD certificate RCA and the cert update CR both mention IBM EM Team and OpenShift. A keyword search finds both when you search "IBM EM." But it has no idea that one is a change request that was executed and the other is a post-incident report written the same day because that change caused an outage. The relationship — and the causal direction — is invisible.

Even a basic graph RAG approach doesn't fully solve this. You can retrieve the top N documents by similarity, dump them into the LLM, and let it sort out the connections. But you're still treating every retrieved document equally, regardless of what kind of information it contains or how it connects to the question. The graph is stored but never actually used during retrieval.

What we need is a system that understands both the content of documents and the connections between them, and uses those connections to navigate to the right answer.

---

## What We Built

The system has two parts: a **5-phase pipeline** that reads and understands every document and constructs a knowledge graph in Neo4j, and a **traversal engine** (`ask.py`) that queries that graph using the structure of the connections themselves — not just keyword similarity.

### The Knowledge Graph

Neo4j stores two types of nodes:

| Node type | What it represents | Example |
|-----------|-------------------|---------|
| `Document` | A single CR or RCA file | `CR/20251111-Update-CPD-Routes.md` |
| `Entity` | Any named thing extracted from the docs — a service, team, cluster, or concept | `IBM EM Team (Organization)`, `zen-minio (Technology)` |

And **9 edge types** across three categories:

**Semantic edges (Document → Document)** — scored by the LLM in Phase 4:

| Edge | Meaning |
|------|---------|
| `EXTENDS` | One document builds upon or extends concepts from the other |
| `CONTRADICTS` | The documents present conflicting information |
| `SUPPORTS` | One document corroborates the findings of the other |
| `REFERENCES` | One document explicitly cites or links to the other |
| `PROVIDES_CONTEXT_FOR` | One document gives the background needed to understand the other |
| `SHARES_DOMAIN_WITH` | Same domain or technology area, no direct relationship |
| `IMPLEMENTS` | One document is the action taken as a result of the other |

**Structural edge (Document → Entity):**

| Edge | Meaning |
|------|---------|
| `MENTIONS` | This document references this entity |

**Temporal edge (Document → Document):**

| Edge | Meaning |
|------|---------|
| `PRECEDED_BY` | This document is dated earlier than the other |

Every semantic edge stores four properties beyond its type: a **strength** score (1–10), a **confidence** level, a **directionality**, and a **description**. The description is the most important — it is a natural-language sentence written specifically to explain, from the perspective of someone who just read the source document, what they will find in the destination document and why following this edge is worth doing. This description is what the traversal engine uses as framing when it retrieves a neighbor.

---

## The 5-Phase Pipeline

Every phase saves its output to `graph-pipeline/staging/` as a JSON file. This means you can resume from any phase without re-running the expensive ones before it. If Phase 4 is interrupted, it resumes from its last checkpoint rather than starting over.

---

### Phase 1 — Independent Extraction (`phase1_extraction.py`)

Reads every `.md` file under `docs/CR/` and `docs/RCA/` and asks the LLM to extract a structured fingerprint from each document **in isolation** — no document sees another yet.

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

**Why only CR/ and RCA/?** The pipeline explicitly ingests only from these two subdirectories. READMEs, index files, templates, and setup guides are excluded. The graph should contain only source-of-truth incident documents.

Saves to: `staging/phase1_extractions.json`

---

### Phase 2 — Global Entity Normalization (`phase2_normalization.py`)

Collects every entity string from every document into one pool and sends them to the LLM in batches to resolve aliases into canonical forms:

```
"IBM EM"        →  "IBM EM Team (Organization)"
"the EM team"   →  "IBM EM Team (Organization)"
"EM Team"       →  "IBM EM Team (Organization)"
```

It also builds a **canonical index** — a map of every canonical entity to the list of documents that mention it, which Phase 3 uses for overlap scoring and Phase 5 uses to create Entity nodes.

**Why global normalization?** If each document normalized its own entities independently, "IBM EM Team" and "EM Team" would never be recognized as the same thing across two documents. Normalization only works if it sees all entity strings at once.

Saves to: `staging/phase2_normalized_entities.json`

---

### Phase 3 — Candidate Filtering (`phase3_candidate_filtering.py`)

With ~294 documents, a naive all-pairs comparison would mean ~43,000 LLM calls in Phase 4. Phase 3 reduces this to a manageable candidate list using two gates:

**Primary gate — entity overlap:**
A document pair passes if they share at least `MIN_ENTITY_OVERLAP` canonical entities, weighted by specificity — an entity appearing in only 2 documents is a much stronger signal than one appearing in 100.

**Secondary gate — semantic similarity (TF-IDF fallback):**
If a pair fails the entity gate but scores above `MIN_SEMANTIC_SIMILARITY` on TF-IDF cosine similarity of their entity+topic text, it still becomes a candidate. This catches cases where two documents describe the same thing using different terminology that Phase 2 normalization didn't unify.

Saves to: `staging/phase3_candidate_pairs.json`

---

### Phase 4 — Pairwise LLM Scoring (`phase4_relationship_scoring.py`)

For each candidate pair, reads both full documents and asks the LLM to score their relationship. The critical output is the **description** field:

```json
{
  "relationship_type": "PROVIDES_CONTEXT_FOR",
  "strength":          8,
  "description":       "Document 2 is the cert update CR executed one day before the voice outage in Document 1 — it contains the specific implementation steps and approval chain for the change that caused the incident.",
  "directionality":    "doc2_to_doc1",
  "confidence":        "high"
}
```

The description is written from the perspective of someone who just finished reading the source document and is deciding whether to follow this edge. It names the actual components, incidents, and decisions involved — not a generic summary. This is what the traversal engine prepends as framing when it retrieves a neighbor document.

**Why full documents?** An RCA tells a story — splitting it into chunks risks cutting the cause from the resolution. The LLM needs to see the whole narrative to correctly judge whether two documents are causally related.

**Resume support:** Results are checkpointed every 10 pairs. If the run is interrupted, restarting from Phase 4 loads the checkpoint and skips already-scored pairs rather than starting over.

Saves to: `staging/phase4_scored_relationships.json`

---

### Phase 5 — Graph Construction (`phase5_graph_construction.py`)

Takes everything from previous phases and writes the final graph to Neo4j:

1. **Filter by quality thresholds:** Drops any relationship scored `NONE`, below `MIN_RELATIONSHIP_STRENGTH`, or with `low` confidence.
2. **Apply degree cap:** For each document, keeps only its top `MAX_DEGREE_PER_NODE` strongest connections, preventing any one document from dominating graph traversal.
3. **Create Document nodes:** One node per unique document, storing filepath, entities, topics, stance, and date.
4. **Create Entity nodes + MENTIONS edges:** Every canonical entity becomes a real Neo4j node. Every document gets a `MENTIONS` edge to each entity it references, making entity-based graph queries a traversal rather than a property scan.
5. **Create PRECEDED_BY temporal edges:** For each related document pair where both documents have a date, creates a directed `PRECEDED_BY` edge from the earlier to the later, with `days_apart` stored on the edge.

Saves to: Neo4j (live graph)

---

## The Traversal Engine (`ask.py`)

The pipeline builds the graph. `ask.py` is how you query it.

The key difference from standard RAG: instead of retrieving the top N documents by embedding similarity and dumping them into the LLM, the traversal engine uses the structure and semantics of the graph edges to navigate to the right documents. The edge types are not metadata — they are the retrieval mechanism.

### Step 1 — Classify the Query Intent

One LLM call classifies what kind of question is being asked:

| Intent | Example | What it needs |
|--------|---------|---------------|
| `causal` | "What caused the cert outage?" | The temporal and contextual chain leading to the incident |
| `resolution` | "How was the ODLM issue fixed?" | The action taken and what decision it came from |
| `timeline` | "What changed in the week before the November incident?" | Chronological ordering of related events |
| `similar` | "Has this kind of pod restart happened before?" | Corroborating cases and domain-shared incidents |

### Step 2 — Map Intent to Edge Type Priorities

Each intent maps deterministically to an ordered list of edge types. The ordering comes from what the edge types mean:

| Intent | Priority order |
|--------|---------------|
| `causal` | `PRECEDED_BY` → `PROVIDES_CONTEXT_FOR` → `SUPPORTS` |
| `resolution` | `IMPLEMENTS` → `REFERENCES` → `PROVIDES_CONTEXT_FOR` |
| `timeline` | `PRECEDED_BY` → `REFERENCES` |
| `similar` | `SUPPORTS` → `SHARES_DOMAIN_WITH` → `EXTENDS` |

This is deterministic — not a learned or probabilistic ranking. Edge types not in the priority list for the current intent are not followed at all.

### Step 3 — Find the Seed Document

TF-IDF over the topics and entities stored on every Document node in Neo4j. The document with the highest cosine similarity to the query becomes the starting point for traversal.

### Step 4 — Best-First Traversal

A priority heap spans all visited nodes. At each step, the globally best available edge is popped — ranked first by its position in the intent's priority list, then by strength as a tiebreaker. This means a strong `PRECEDED_BY` edge discovered two hops in will be followed before a weak `SUPPORTS` edge available from the current node.

Traversal stops when either the maximum document count (5) or maximum hop depth (4) is reached, or when no qualifying edges remain.

### Step 5 — Edge Description as Framing

When a neighbor document is retrieved via a traversal hop, its raw text is not injected into the LLM context alone. The edge description is prepended:

```
[Why this document is here: Document 2 is the cert update CR executed one day
before the voice outage in Document 1 — it contains the specific implementation
steps and approval chain for the change that caused the incident.]

[Full document text follows...]
```

The LLM knows why it is reading this document and what role it plays in the answer before it reads a single word of content.

### Step 6 — Answer Generation

The ordered, framed documents are passed to the LLM with the original query and an instruction to answer specifically using only what is in the provided documents. The answer references actual document names, dates, and technical details — not generic summaries.

---

## Running the System

### Build the graph

```bash
cd graph-pipeline
pip install neo4j scikit-learn requests
python3 main.py
```

Resume from a specific phase (e.g. after Phase 4 was interrupted):
```bash
python3 main.py --from-phase 4
```

Re-run only graph construction after tuning thresholds:
```bash
python3 main.py --from-phase 5
```

### Query the graph

```bash
# Single question
python3 ask.py "What caused the CPD certificate outage?"

# Interactive mode
python3 ask.py
```

---

## Prerequisites

### Neo4j
Download Neo4j Desktop, create a local database, and start it. Set your credentials in `graph-pipeline/config.py`:
```python
NEO4J_URI      = "bolt://localhost:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "your-actual-password"
```

### LM Studio
Load a model (we use Granite 4.1 8B, identifier `ibm/granite-4.1-8b`) and start the local server on port 1234. Set the model identifier in `graph-pipeline/config.py`:
```python
LM_STUDIO_MODEL = "ibm/granite-4.1-8b"
```

---

## Tunable Parameters

All thresholds live in `graph-pipeline/config.py`:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `MIN_ENTITY_OVERLAP` | `2` | Min shared entities for Phase 3 primary gate |
| `MIN_SEMANTIC_SIMILARITY` | `0.25` | Min TF-IDF cosine similarity for Phase 3 fallback gate |
| `MIN_RELATIONSHIP_STRENGTH` | `4` | Min LLM strength score (1–10) for Phase 5 to write an edge |
| `MAX_DEGREE_PER_NODE` | `10` | Max edges per document node |

Traversal parameters live in `graph-pipeline/ask.py`:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `MAX_DOCS` | `5` | Maximum documents collected per query |
| `MAX_HOPS` | `4` | Maximum traversal depth from the seed |

---

*Built for UPS Watson Incident Intelligence — June 2026*
