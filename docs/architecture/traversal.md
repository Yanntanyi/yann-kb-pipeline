# UPS Watson Incident Intelligence: How Traversal Works

*A complete, ground-up explanation of how we answer a question by walking the knowledge graph. Read this top to bottom and you'll understand every moving part — what it does, why it's there, and why it beats the obvious alternatives.*

---

## 1. The one-sentence version

When you ask a question, we **find a few good documents to start from**, then **walk the connections between documents** — following the kinds of links that actually answer your type of question — and hand the LLM a small, ordered set of documents where **each one comes with a note explaining why it's relevant**. The LLM then writes the answer using only those documents.

That's the whole idea. The rest of this document explains each piece slowly.

---

## 2. Why we don't just do "normal" search (the problem we're solving)

The obvious way to build a Q&A system over documents is **flat retrieval**, also called standard RAG:

1. Turn the question into a vector (a list of numbers that captures its meaning).
2. Find the N documents whose vectors are closest.
3. Dump all N into the LLM and ask for an answer.

This works okay for trivia. It fails for incident intelligence in two specific ways:

**Problem 1 — It's blind to *structure*.** Imagine two documents: a Change Request that updated a TLS certificate, and an RCA written the next day about a voice outage. Flat search sees that both mention "certificate" and "OpenShift," so it retrieves both. But it has **no idea that one *caused* the other**. It treats them as two equally-weighted blobs of text. The single most important fact — that the change triggered the outage — is invisible, because that fact lives in the *relationship between* the documents, not inside either one.

**Problem 2 — It's trapped by vocabulary.** Flat search only finds documents that share words (or word-meanings) with your question. A document that is *one hop away* and directly relevant — but happens to describe things in different words — gets missed entirely. If the RCA says "pod restart" and the related incident says "container crash-loop," flat search may never connect them.

The fix for both problems is the same: **use the connections between documents as the actual search mechanism**, not just as decoration.

---

## 3. The core insight: the *type* of a connection already tells you a lot

In our graph (built by the 5-phase pipeline and stored in Neo4j), documents are connected by **typed edges**. The type isn't a vague "these are related" — it tells you the *role* one document plays for another:

| Edge type | What it means in plain English |
|---|---|
| `PRECEDED_BY` | This document happened *before* that one (time/causal chain). |
| `PROVIDES_CONTEXT_FOR` | This document is the *background* you need to understand that one. |
| `IMPLEMENTS` | This document is the *action taken* as a result of that one. |
| `REFERENCES` | This document explicitly *points to* that one. |
| `SUPPORTS` | This document *backs up / corroborates* that one. |
| `EXTENDS` | This document *builds on* that one. |
| `SHARES_DOMAIN_WITH` | Same topic area, but no direct relationship. |
| `CONTRADICTS` | The two documents *disagree*. |

Here's why this matters so much: **these meanings are fixed.** `PRECEDED_BY` *always* means a time ordering. `IMPLEMENTS` *always* means an action-from-a-decision. They were decided once, at graph-build time, by an LLM that read both full documents carefully. At question time we don't have to re-guess them — we can just *trust the labels* and use them to navigate. This is the difference between a road map where every road is labeled "highway / side street / dead end" versus a map where every road just says "road."

Each edge also carries a **strength score (1–10)** and — most importantly — a **one-sentence description** written specifically to explain *why following this edge is worth it*. Hold onto the description idea; it's the secret weapon and we'll come back to it in Section 8.

---

## 4. The journey of a question (the map)

Every question goes through six steps. Here's the whole pipeline before we zoom in:

```
Your question
   │
   ▼
[1] Figure out what KIND of question it is        → "intent"      (1 LLM call)
   │
   ▼
[2] Turn that intent into a ranked list of         → "edge plan"  (no LLM, instant)
    which edge types to follow first
   │
   ▼
[3] Find a few good documents to start from        → "seeds"      (search, no LLM)
   │
   ▼
[4] Walk the graph from those seeds, always         → "path"      (no LLM, just graph
    taking the most promising connection                            + math)
   │
   ▼
[5] Attach the "why" note to each document found   → "context"    (no LLM)
   │
   ▼
[6] Ask the LLM to write the answer from that       → "answer"    (1 LLM call)
    small, labeled set of documents
```

Notice the thing that makes this cheap and fast: **the whole graph walk uses zero LLM calls.** We only call the LLM twice total — once to understand the question, once to write the answer. Everything in between is fast graph lookups and arithmetic. (We'll defend this choice hard in Section 9.)

The code lives in `graph-pipeline/ask.py` (the `KnowledgeGraphQuerier` class). Hybrid search lives in `graph-pipeline/es_handler.py`.

---

## 5. Step 1 — Understanding the question (intent classification)

**What it does:** One LLM call sorts your question into exactly one of four buckets.

| Intent | Example question | What it really needs |
|---|---|---|
| `causal` | "What caused the cert outage?" | The chain of events leading up to it |
| `resolution` | "How was the ODLM issue fixed?" | The action taken and the decision behind it |
| `timeline` | "What changed the week before the November incident?" | Events in time order |
| `similar` | "Has this kind of pod restart happened before?" | Comparable past cases |

**Why we do it:** Different questions need different kinds of connections. A "what caused this" question wants to walk *backwards in time* and pick up *context*. A "how was it fixed" question wants to find the *action that was taken*. If we treated every question the same, we'd follow the wrong links and bring back the wrong documents.

**Why it's the only "fuzzy" step — and why that's good:** This is the single place in the whole system where we let the LLM make a judgment call that could be wrong. We deliberately keep it small: four choices, one short call. If it ever misfires, the failure is **obvious and fixable in one spot** — you can look at the printed intent and immediately see "oh, it called a causal question 'similar.'" Compare that to systems where uncertainty is smeared across a hundred little scoring decisions you can never inspect. We concentrate the risk into one visible, debuggable place. (And if the call fails entirely, the code falls back to `similar`, the most general bucket — see `classify_intent`.)

---

## 6. Step 2 — Turning intent into a search plan (edge priorities)

**What it does:** Each intent maps to an *ordered* list of edge types — the order we prefer to follow them in. This is a plain lookup table (`EDGE_PRIORITIES` in `ask.py`), no LLM, instant:

| Intent | Follow edges in this order |
|---|---|
| `causal` | `PRECEDED_BY` → `PROVIDES_CONTEXT_FOR` → `SUPPORTS` |
| `resolution` | `IMPLEMENTS` → `REFERENCES` → `PROVIDES_CONTEXT_FOR` |
| `timeline` | `PRECEDED_BY` → `REFERENCES` |
| `similar` | `SUPPORTS` → `SHARES_DOMAIN_WITH` → `EXTENDS` |

**How to read it:** For a `causal` question, a `PRECEDED_BY` edge is the most valuable thing we can follow (it walks the causal/time chain), so it's first. If there's no `PRECEDED_BY` available, a `PROVIDES_CONTEXT_FOR` edge is the next best thing, and so on.

**Two important consequences:**

- **Edge types *not* in the list are never followed.** For a `causal` question we simply don't walk `SHARES_DOMAIN_WITH` edges — they're noise for that question. (In the code, we only ever *fetch* edges whose type is in the plan, so off-plan edges never even enter consideration.)
- **The ordering comes from *meaning*, not from training.** We didn't learn these weights from data or tune them on examples. They follow directly from what the edge types mean. That makes the system explainable: you can justify every ordering with one sentence ("a causal question follows time/causal edges first — obviously"). There's nothing to retrain, drift, or recalibrate.

---

## 7. Step 3 — Finding where to start (seed selection)

We have to start the walk *somewhere*. The starting documents are called **seeds**. Getting good seeds matters: if you start in the wrong neighborhood of the graph, no amount of clever walking saves you. We do three things to get this right.

### 7a. Hybrid search: two kinds of "relevant" at once

To find seeds we search a separate Elasticsearch index that holds every document's full text plus a **dense embedding** (a meaning-vector from the `granite-embedding` model). We run **two different searches** and combine them:

- **BM25 (keyword search):** classic text matching. It's great at *exact* things — error codes, service names like `zen-minio`, ticket numbers, specific jargon. If your question says "ODLM," BM25 nails the documents that literally say "ODLM."
- **Dense / vector search (meaning search):** matches by *meaning*, not exact words. It catches "pod restart" ≈ "container crash-loop," or a question phrased completely differently from the document. This is what fixes the "trapped by vocabulary" problem from Section 2.

**Why both?** Because each one fails where the other shines. Keyword search misses paraphrases; meaning search misses rare exact tokens (it can blur a precise error code into "something error-ish"). Running both and combining them means we catch a document whether it matches your *words* or your *intent*. This is called **hybrid retrieval**, and it's strictly more robust than either half alone.

### 7b. RRF: a fair way to combine the two rankings

Now we have two ranked lists (one from BM25, one from dense) and need a single list. We use **Reciprocal Rank Fusion (RRF)**. The intuition is dead simple:

> Each search "votes" for documents based on how high it ranked them. A document ranked #1 gets a big vote, #2 a slightly smaller vote, and so on (the formula is `1 / (k + rank)`). We add up each document's votes across both lists. Whatever scores highest overall wins.

Why RRF instead of, say, averaging the raw scores? Because BM25 scores and vector-similarity scores are on **totally different scales** — you can't just average them, it's apples and oranges. RRF throws away the raw numbers and only looks at *rank position*, which is comparable across both. The practical payoff: **a document that ranks decently in *both* searches beats a document that ranks #1 in only one.** That "agreed on by both methods" document is almost always the right place to start. (The `k` constant, `ES_RRF_K`, just controls how steeply top ranks are favored; we use 60, a standard default. We also fuse the lists ourselves in Python rather than relying on a specific Elasticsearch feature, so it works on any version — see `_rrf_fuse` in `es_handler.py`.)

### 7c. Multi-seed: don't bet everything on one document

We don't take just the single best document — we take the top **`NUM_SEEDS`** of them (default **3**) and start the walk from *all of them at once*.

**Why:** Picking one seed is a single point of failure. If the top result is *slightly* off (and search is never perfect), the entire traversal starts in the wrong place and the answer is doomed — and you'd never know, because the machinery downstream still "works," it just works on the wrong neighborhood. Starting from three anchors hedges that bet. If one is a dud, the other two still pull the walk toward the right region of the graph. It costs us almost nothing (we just begin the walk with three starting points instead of one), and it dramatically reduces the "wrong start = wrong answer" risk.

### 7d. A safety net

If Elasticsearch is down, empty, or returns nothing, we fall back to a simpler built-in search (TF-IDF over each document's topics and entities) and start from a single seed — see `_find_seed_tfidf`. The system still answers; it just loses the hybrid/multi-seed advantages until ES is back. This means the query tool keeps working even before you've built the search index.

---

## 8. Step 4 — Walking the graph (best-first traversal)

This is the heart of the system. We have our seeds and our edge plan. Now we walk.

### 8a. The core technique: a "best-first" walk with a priority queue

Picture every document we *could* go to next as a candidate sitting in a waiting line. But it's not a normal line — it's a **priority queue** (a "heap"): the most promising candidate is always at the front, no matter when it was added.

The loop is:

1. Look at every edge leaving our current set of documents that matches the edge plan. Add each as a candidate to the queue.
2. Pull the **single best candidate from the entire queue** and go to that document. Add it to our results (the "path").
3. From this new document, look at *its* edges and add those as new candidates to the same queue.
4. Repeat.

**What does "best" mean?** Each candidate is ranked by two things, in order:

1. **Its edge type's position in the plan** (Section 6). A `PRECEDED_BY` edge (plan position 0) always beats a `PROVIDES_CONTEXT_FOR` edge (position 1) for a causal question.
2. **The edge's strength score (1–10)** as a tie-breaker. Among two edges of the same type, follow the stronger one first.

(In code this is a heap of tuples `(rank, -strength, counter, edge)`; Python's heap keeps the smallest at the front, so "lowest plan-position, then highest strength" naturally floats to the top. The `counter` is just a unique number to break exact ties cleanly without the code trying to compare the edge objects themselves.)

### 8b. Why "best-first" and not the usual alternatives

This is worth understanding deeply because it's a question people *will* ask you.

- **It's not breadth-first (BFS).** BFS explores everything one step away, then everything two steps away — spreading your limited budget thinly in all directions, including useless ones. We don't want "everything nearby," we want "the best chain."
- **It's not blind depth-first (DFS) either.** A naive DFS commits to one path and charges down it, even if a much better edge was sitting right at the start. The danger of DFS is *over-committing* to a path that turns bad.
- **Best-first is the smart middle.** Because the queue is **global** — it holds candidates from *every* document we've visited, not just the current one — we always take the genuinely best available move *anywhere in the explored frontier*. So we get the focus of DFS (we follow promising chains deep) without its recklessness (a great edge discovered two steps back can still get picked next if it's better than anything on the current path). We commit to good paths, but we never blindly commit.

In one line: **we always spend our next step on the most valuable connection available anywhere, judged by what the edge *type* promises and how *strong* it is.**

### 8c. We look both ways down every edge

Edges have a direction (e.g., `PRECEDED_BY` points from the earlier document to the later one). But the document that answers your question might be on *either* end. Classic example: you're standing on an RCA about an outage and asking what caused it. The Change Request that triggered it points *into* the RCA (`CR —PRECEDED_BY→ RCA`). If we only looked at edges *leaving* the RCA, we'd miss the very thing we're hunting for.

So at each document we fetch **both outgoing and incoming** edges (see `get_neighbors`). If the same neighbor is reachable both ways, we keep the **stronger** edge and drop the duplicate.

### 8d. We never visit the same document twice

We keep a `visited` set. Once a document is in our path, it can't be added again, and edges leading back to it are ignored. This prevents loops (A→B→A→B…) and stops one document from being counted multiple times.

### 8e. When we stop

The walk ends as soon as **any** of these is true:

- **We've collected enough documents** — `MAX_DOCS` (default **6**: the ~3 seeds plus a few hops). Enough to answer well, small enough to keep the LLM focused and the answer grounded.
- **We've taken enough hops** — `MAX_HOPS` (default **4**) graph steps beyond the seeds. Stops the walk from wandering far from where it started.
- **The queue is empty** — there are simply no more on-plan edges worth following. The graph itself is telling us "there's nothing more structurally relevant here," and we listen.

The result is a **small, ordered list of documents**, chosen because they're the *right kind* of information for your question — not because they happened to share words with it.

---

## 9. Step 5 — Telling the LLM *why* each document is here (edge-description framing)

This is the feature that makes the answers feel like they were written by someone who actually understands the incident, and it's the part most worth bragging about.

Remember from Section 3 that every edge stores a **one-sentence description**, written at build time by an LLM that read both full documents. It's not a generic summary — it's directional framing that answers: *"If I just finished reading document A and I follow this edge, what will I find in document B, and why should I care?"* For example:

> *"Document 2 is the change request that updated the CPD certificate routes one day before the voice outage described in Document 1, containing the specific implementation steps and approval chain for that change."*

When we hand a document to the LLM, we **don't just paste the raw text.** We prepend its edge description as a label:

```
[Document 2: CR/20251111-Update-CPD-Routes.md]
[Why this document is here: Document 2 is the change request that updated the CPD
certificate routes one day before the voice outage in Document 1, containing the
specific implementation steps and approval chain for that change.]

<full text of the document...>
```

**Why this is powerful:** The LLM reads each document already knowing *what role it plays in the story* before it reads a single word of content. It's the difference between handing someone a stack of papers versus handing them a stack where each one has a sticky note saying "this is the change that caused it" or "this is the background you need first." The model spends its effort *using* the documents instead of guessing how they fit together. And because the description was written knowing the relationship, it captures connections the raw text alone never states (the raw CR never says "this caused the outage in the other document" — only the edge does).

The seed documents have no such note (they weren't reached by following an edge — they were the starting points), so they're presented with just their filename.

---

## 10. Step 6 — Writing the answer (grounded generation)

The final, ordered, labeled set of documents goes to the LLM with the original question and tight instructions (`generate_answer`):

- Answer **only** from the provided documents.
- Cite specific document names, dates, components, technical details.
- If the documents don't fully answer the question, **say so explicitly** rather than inventing.

This is the second (and last) LLM call. We give it a generous token budget (4096) because our model (gpt-oss) is a *reasoning* model — it "thinks" in a hidden scratchpad before writing, and we don't want it running out of room mid-thought (this is exactly why a tiny budget once produced empty answers; see the LLM-client notes).

The output cites real documents, real dates, and the actual causal/temporal connections — because we *navigated* to those connections instead of hoping they'd fall out of a pile of similar-looking text.

---

## 11. Why this is genuinely good — and how to defend it

If someone challenges you with "why not just do X instead," here are the honest, confident answers.

**"Why not just retrieve the top-K documents by vector similarity and dump them in?"**
Because that's structure-blind and vocabulary-bound (Section 2). It can't tell that one document *caused* another — that fact lives in the relationship, which flat retrieval discards. And it misses one-hop-away documents that are relevant but worded differently. We use both the content (for seeds) *and* the structure (for the walk), so we get the relevant-by-meaning documents *and* the relevant-by-relationship ones.

**"Why not call the LLM at every hop to score which edge to follow?"**
Two reasons. First, **cost and speed**: a deep walk would mean dozens of extra LLM calls per question, turning a 2-call query into a slow, expensive one. We keep it to exactly 2 calls total. Second, **we don't need to**: the edge *type* is already a reliable, pre-computed signal of what's down that road, and the edge *description* gives us all the query-relevant nuance at the moment it actually matters — when the LLM reads the document. So we get the benefit of "the LLM understands why this edge matters" without paying for an LLM call on every single hop.

**"Isn't a deterministic edge plan too rigid? Shouldn't it be learned/weighted?"**
The rigidity is the point. The orderings come from what the edge types *mean*, which doesn't drift, doesn't need retraining, and can be explained in one sentence each. A learned scoring function is a black box that needs data, can be miscalibrated, and fails invisibly. We deliberately put the *only* judgment call (intent) in one inspectable place (Section 5) and made everything after it transparent.

**"Best-first walking sounds like it could get stuck on a bad path."**
It can't over-commit, because the priority queue is **global** (Section 8b): the next step is always the best edge available across *everything* we've explored, so a great connection found earlier can always be taken later. We get DFS-style focus without DFS-style tunnel vision.

**"What if the single best starting document is wrong?"**
That's exactly why we use **multiple seeds** (Section 7c). One wrong starting point can't sink the whole query when two others are pulling toward the right neighborhood.

**"BM25 is old / embeddings are fuzzy — why not just pick one?"**
Because they fail in opposite situations (Section 7a). Keyword search owns exact tokens; meaning search owns paraphrases. Hybrid + RRF gives us both, and RRF specifically rewards the documents both methods agree on — which are the safest seeds.

**"What if the graph has no relevant edges for my question?"**
Then the walk stops early (empty queue) and we answer from the seeds alone, and the LLM is instructed to say when the documents don't fully cover the question. The system degrades gracefully instead of fabricating links.

---

## 12. The knobs (and what turning them does)

Everything tunable, in one place:

| Setting | Where | Default | What it controls |
|---|---|---|---|
| `NUM_SEEDS` | `config.py` | 3 | How many starting documents. More = more robust start, but eats into the document budget. |
| `MAX_DOCS` | `ask.py` | 6 | Total documents collected. Bigger = more context but a less focused (and slower) answer. |
| `MAX_HOPS` | `ask.py` | 4 | How far the walk can roam past the seeds. |
| `EDGE_PRIORITIES` | `ask.py` | (table in §6) | Which edge types each intent follows, and in what order. |
| `ES_USE_DENSE` | `config.py` | true | Hybrid (BM25 + meaning). Turn off for keyword-only. |
| `ES_RRF_K` | `config.py` | 60 | How steeply RRF favors top ranks when fusing the two searches. |

---

## 13. The design philosophy in three lines

1. **Use the structure.** The relationships between documents *are* the answer to most incident questions; treat edges as the search mechanism, not metadata.
2. **Trust deterministic signals; isolate the one fuzzy decision.** Edge types and the edge plan are fixed and explainable. The only judgment call — intent — is small, single, and inspectable.
3. **Spend LLM calls where they count.** Two calls total: one to understand the question, one to write the answer. Everything in between is fast graph math, and every retrieved document arrives pre-labeled with *why it's there*.

That's the entire traversal system. If you understand these thirteen sections, you can explain any part of it, justify every design choice, and hold your ground against "but wouldn't X be better" — because for the ways that matter here, it wouldn't.

---

*Built for UPS Watson Incident Intelligence.*
