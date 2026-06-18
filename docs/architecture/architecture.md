# UPS Watson Incident Intelligence: Architecture

*What the system is, how it's split into pieces, and — file by file — what every piece of code does and why it's built that way. For the deep dive on how a question is actually answered, see [traversal.md](traversal.md); this document is the map of the whole codebase.*

---

## 1. The problem

Every cloud incident at UPS produces an **RCA** (Root Cause Analysis). Every controlled change produces a **Change Request (CR)**. Over time you accumulate hundreds of these documents sitting in folders. When a new incident hits, nobody can quickly answer: *Has this happened before? Which change caused it? What was done to fix the last one? What else broke that week?* You start from zero every time.

We want a system that can answer those questions by **understanding both what each document says and how the documents connect to each other.**

---

## 2. Why not just plain RAG

The obvious approach — embed the question, grab the most similar documents, hand them to an LLM — fails here in two ways: it's **blind to structure** (it can't tell that one document *caused* another; that fact lives in the relationship, not the text) and it's **trapped by vocabulary** (it misses a directly-relevant document that happens to use different words). The fix is to make the **connections between documents** the actual search mechanism. The full argument is in [traversal.md §2](traversal.md); here we just note that this is *why* the system is shaped the way it is.

---

## 3. The system in two halves (plus its infrastructure)

```
                     BUILD TIME (once, then on new docs)
   docs/CR + docs/RCA ──► [5-phase pipeline] ──► Knowledge graph in Neo4j
                                                        │
                                                        ▼
                                              [index_es.py] ──► Elasticsearch
                                                                  (search index)
   ─────────────────────────────────────────────────────────────────────────
                     QUERY TIME (every question)
   your question ──► [ask.py] ──► seeds from Elasticsearch
                                ──► walk the graph in Neo4j
                                ──► grounded answer from the LLM
```

- **The build pipeline** (`main.py` + `phase1`–`phase5`) reads every document and constructs the graph in Neo4j. Expensive, run rarely.
- **The query engine** (`ask.py`) answers questions by walking that graph. Cheap, run constantly.
- **Three external systems** support both: **Neo4j** (stores the graph), **Elasticsearch** (finds where to start a query), and **an LLM** (gpt-oss on watsonx, or local Granite) for the reading/judging/writing.

The rest of this document walks every file.

---

## 4. The data model (what's in the graph)

Two kinds of **nodes**:

| Node | Represents | Example |
|---|---|---|
| `Document` | One CR or RCA file | `CR/20251111-Update-CPD-Routes.md` |
| `Entity` | A named thing (service, team, cluster, concept) | `IBM EM Team (Organization)` |

The **edges** carry the meaning. Document→Document semantic edges (`EXTENDS`, `CONTRADICTS`, `SUPPORTS`, `REFERENCES`, `PROVIDES_CONTEXT_FOR`, `SHARES_DOMAIN_WITH`, `IMPLEMENTS`) are scored by the LLM in Phase 4 and each carries a **strength (1–10)**, a **confidence**, a **directionality**, and a one-sentence **description** explaining why the link matters. A `MENTIONS` edge connects a Document to each Entity it references. A `PRECEDED_BY` edge connects two dated, related documents in time order. (What each edge *means* and how traversal uses it is in [traversal.md §3 and §6](traversal.md).)

---

## 5. Foundation files (shared by everything)

### `config.py` — the single control panel
Every tunable lives here: ingest paths, the Phase 3/4/5 thresholds, the LLM provider switch, watsonx + LM Studio settings, embedding model + dimension, Elasticsearch settings, seed count, and Neo4j credentials. A small `_env()` helper reads each setting from an environment variable (accepting both `WATSONX_*` and `REPORT_WATSONX_*` names) and falls back to a sensible default.

**Why it's built this way:** putting every knob in one file means you change *behavior* without touching *logic*, and the env-var support is what makes the "write code on one laptop, run it on another" workflow painless — you set keys in the environment, not in source.

### `llm_client.py` — one LLM interface, two backends
Defines two clients that expose the **same three methods** — `generate_json()`, `generate_text()`, `embed()` — so nothing else in the codebase knows or cares which LLM is in use:

- **`WatsonxClient`** talks to IBM watsonx.ai (default; runs `gpt-oss-120b`). watsonx is *not* OpenAI-compatible, so this class handles the things that make it work: exchanging your API key for an **IAM bearer token** and caching it (refreshing ~1 min before it expires, behind a lock so parallel calls are safe), building the watsonx-specific request body (`model_id` + `project_id`), and **retrying** on expired-token (401) and rate-limit/transient (429/5xx) errors with exponential backoff.
- **`LMStudioClient`** talks to a local LM Studio server (e.g. Granite). Free, offline, OpenAI-style.
- **`get_llm_client()`** returns whichever one `config.LLM_PROVIDER` selects.

Two details exist specifically because gpt-oss is a **reasoning model**: `_extract_json_object()` tolerates a model that wraps its JSON in prose, and `_chat()` raises a clear "empty content — increase max_tokens" error instead of crashing cryptically when the model spends its whole token budget thinking. (Both were added after we hit exactly those failures.)

**Why it's built this way:** the unified interface means swapping the entire system between cloud gpt-oss and local Granite is a one-flag change (`LLM_PROVIDER`). The retry/backoff logic is what makes Phase 4's parallelism safe. The reasoning-model handling is hard-won.

### `neo4j_handler.py` — the graph database wrapper
Wraps the Neo4j driver with one method per operation: create Document/Entity nodes, create relationship/MENTIONS/PRECEDED_BY edges, run read queries (`query_graph`), and set up schema constraints. Every write uses `MERGE`.

**Why it's built this way:** `MERGE` makes the whole pipeline **idempotent** — you can re-run any phase without creating duplicate nodes or edges. And the relationship type is **whitelisted** before being put into a query string (Neo4j can't accept a relationship type as a safe parameter), which prevents injection without needing the APOC plugin.

### `es_handler.py` — the hybrid search engine
A thin REST wrapper around Elasticsearch (using plain `requests`, no extra dependency). It builds the index (BM25 text fields + an optional dense-vector field sized to `EMBEDDING_DIM`), bulk-loads documents, and runs **two searches** — keyword (BM25) and meaning (dense kNN) — then fuses their rankings with **Reciprocal Rank Fusion** done in Python (`_rrf_fuse`).

**Why it's built this way:** keyword and meaning search fail in opposite situations, so combining them is more robust than either (full reasoning in [traversal.md §7](traversal.md)). Doing RRF ourselves in Python — rather than via an Elasticsearch feature — means it works on any ES version regardless of license tier. Documents are keyed by their Neo4j hash, so a search result maps straight back to a graph node.

---

## 6. The build pipeline (constructs the graph)

### `main.py` — the orchestrator
Runs the five phases in order. Each phase saves its output to `staging/` as JSON; on a later run, earlier phases load from staging instead of recomputing. `--from-phase N` resumes from phase N.

**Why it's built this way:** the phases are expensive (lots of LLM calls). Staging + resume means an interruption — or a deliberate "just re-run Phase 5 with new thresholds" — never forces you to redo the costly earlier work.

### `phase1_extraction.py` — read each document alone
Reads every `.md` under `docs/CR/` and `docs/RCA/` (skipping templates/READMEs), gives each a **SHA-256 content hash** (so identical files are processed once), and asks the LLM to extract a structured fingerprint — `entities`, `topics`, `stance`, `date` — from **each document in isolation**.

**Why in isolation:** if documents were processed together, the first ones would anchor the vocabulary and bias how every later entity gets named. Processing each alone, then normalizing globally in Phase 2, prevents that first-mover bias.

### `phase2_normalization.py` — unify entity names
Pools every entity string from every document and asks the LLM, in **small batches**, to map aliases to one canonical form (`"IBM EM"`, `"EM Team"` → `"IBM EM Team (Organization)"`). It then builds a **canonical index** (each entity → which documents mention it + a count), which Phases 3 and 5 both use.

**Why global:** aliases can only be recognized as the same thing if they're seen *together* — per-document normalization would never connect "IBM EM" in one file to "EM Team" in another. **Why small batches (30):** gpt-oss is a reasoning model that spends tokens thinking before writing the mapping; a large batch overflows the output limit and comes back empty. Small batches keep each response safely under the ceiling.

### `phase3_candidate_filtering.py` — narrow down the pairs to score
With ~300 documents there are ~43,000 possible pairs — far too many to send to the LLM. This phase keeps only **plausible** pairs, using two gates: a pair passes if it shares at least `MIN_ENTITY_OVERLAP` canonical entities (**primary gate**), or — only checked if the first fails — if its TF-IDF text similarity exceeds `MIN_SEMANTIC_SIMILARITY` (**semantic fallback gate**, which catches same-topic pairs that use different words). Rare entities count more (IDF-like specificity weighting) when sorting candidates. Each candidate records which gate it passed.

**Why it's built this way:** it's the cost-control valve for the whole pipeline — it turns ~43k expensive LLM calls into a manageable candidate set. (We analyzed the gate thresholds with `analyze_phase4.py` and found loosening/tightening them trades real edges for noise, so they're left as-is — see §9.)

### `phase4_relationship_scoring.py` — judge each pair
For every candidate pair it reads **both full documents** and asks the LLM for: the relationship `type`, a `strength` (1–10), the `directionality`, a `confidence`, and — most importantly — a one-sentence `description` written as *directional framing* ("if you just read document A and follow this edge, here's what's in B and why it matters"). Pairs run through a **thread pool** (`PHASE4_CONCURRENCY` at a time), results are checkpointed every 10, and a re-run **skips already-scored pairs**.

**Why full documents:** an RCA tells a story; chunking it risks separating the cause from the effect, so the LLM needs the whole narrative to judge the relationship. **Why the description matters so much:** it's the exact text the query engine later shows the LLM to explain why a retrieved document is relevant — it captures connections the raw text never states. **Why concurrent:** scoring tens of thousands of pairs one-at-a-time took ~18 hours; running 8 in parallel cuts that to a few hours with identical results (the LLM client's retry/backoff makes parallel calls safe). **Why `NONE`:** a conservative "no real relationship" verdict that Phase 5 drops.

### `phase5_graph_construction.py` — write the final graph
Takes everything above and builds the Neo4j graph in five steps: (1) **filter** out `NONE`, low-strength (`< MIN_RELATIONSHIP_STRENGTH`), and low-confidence relationships; (2) apply a **degree cap** keeping only each document's top `MAX_DEGREE_PER_NODE` strongest edges; (3) create Document nodes; (4) create the Document→Document edges (respecting each one's directionality); (5) create Entity nodes + `MENTIONS` edges, and `PRECEDED_BY` time edges between dated, already-related pairs.

**Why the degree cap:** without it, a document that mentions common entities would connect to everything and dominate every traversal. Keeping only the strongest few edges per node keeps the graph navigable and the final quality high — which is also *why loosening Phase 3 doesn't hurt graph quality*: weak edges that slip through get discarded here anyway. **Why `PRECEDED_BY` only between already-related pairs:** we want "this change preceded this incident," not "every old document preceded every new one."

---

## 7. The query side (answers questions)

### `index_es.py` — build the search index
After the graph exists, this pulls every Document node **from Neo4j**, reads its full text from disk, computes a dense embedding (if hybrid search is on), and bulk-loads it all into Elasticsearch.

**Why pull from Neo4j (not the files directly):** it guarantees the Elasticsearch document IDs are the same hashes as the graph nodes, so a search hit maps directly to a node to start traversal from. Run it after the pipeline, and re-run it whenever the corpus changes.

### `ask.py` — the traversal engine
The thing you actually run to ask a question. In short: classify the question's intent (1 LLM call) → turn that into an ordered edge plan → get the top seed documents from Elasticsearch hybrid search (with a TF-IDF fallback if ES is down) → walk the graph best-first, collecting documents → attach each edge's "why it's here" description → write a grounded answer (1 LLM call). Only **two LLM calls** per question; the walk itself is pure graph math.

This is the heart of the system and has its own complete, ground-up explanation in **[traversal.md](traversal.md)** — the seed selection, the best-first heap, the both-directions edge lookup, the framing, and how to defend every design choice.

---

## 8. Tooling

### `analyze_phase4.py` — tune thresholds from evidence
Reads the Phase 4 results and reports the valid-vs-`NONE` split, the relationship-type mix, strength distribution, and the Phase 3 gate signals — plus a **what-if grid** showing, for various tightened thresholds, how much noise you'd remove versus how many real (and *strong*) edges you'd lose.

**Why it exists:** so Phase 3 thresholds are set from real data, not guesses. Running it is exactly what told us the gates are best left alone (tightening them dropped more strong edges than noise).

---

## 9. How it all runs

```bash
cd graph-pipeline
source venv/bin/activate

# one-time setup: which LLM, and its credentials
export LLM_PROVIDER=watsonx                       # or 'lmstudio' for local Granite
export WATSONX_API_KEY=...  WATSONX_PROJECT_ID=...

# 1. Build the graph (rarely)
python3 main.py                 # or --from-phase N to resume
# 2. Build the search index (after the graph exists / when docs change)
python3 index_es.py --recreate
# 3. Ask questions (constantly)
python3 ask.py "What caused the CPD certificate outage?"
```

The `LLM_PROVIDER` switch is the payoff of the unified client: you can build the graph on local Granite (free) and answer queries on gpt-oss (better writing), or run everything on one backend — without changing any code.

---

## 10. Tunable parameters

| Parameter | File | Default | Controls |
|---|---|---|---|
| `LLM_PROVIDER` | config.py | `watsonx` | Which LLM backend the whole system uses. |
| `PHASE4_CONCURRENCY` | config.py | 8 | How many pairs Phase 4 scores in parallel. |
| `MIN_ENTITY_OVERLAP` | config.py | 2 | Phase 3 primary gate: shared entities needed. |
| `MIN_SEMANTIC_SIMILARITY` | config.py | 0.25 | Phase 3 fallback gate: TF-IDF similarity needed. |
| `MIN_RELATIONSHIP_STRENGTH` | config.py | 4 | Phase 5 drops edges weaker than this. |
| `MAX_DEGREE_PER_NODE` | config.py | 10 | Max edges kept per document (anti-hub). |
| `EMBEDDING_DIM` | config.py | 768 | Must match the embedding model (sets the ES mapping). |
| `ES_USE_DENSE` | config.py | true | Hybrid (BM25 + meaning) vs keyword-only seeds. |
| `NUM_SEEDS` | config.py | 3 | How many starting documents a query uses. |
| `MAX_DOCS` / `MAX_HOPS` | ask.py | 6 / 4 | How many documents a query collects / how far it walks. |

---

## 11. Design philosophy

1. **Two halves, cleanly split.** A slow build pipeline that *understands* the documents and turns them into a graph; a fast query engine that *navigates* that graph. Each is simple on its own.
2. **The structure is the value.** Most incident questions are answered by the relationships *between* documents, so we make those relationships first-class — scored, described, and used as the retrieval mechanism, not metadata.
3. **One judgment-heavy step, isolated.** The LLM does the hard reading and judging at build time (extraction, normalization, relationship scoring); at query time the heavy lifting is fast deterministic graph traversal plus just two LLM calls.
4. **Swappable, resumable, idempotent.** One flag swaps the LLM; staging makes every phase resumable; `MERGE` makes every write safe to repeat. The system is built to be re-run.

---

*Built for UPS Watson Incident Intelligence.*
