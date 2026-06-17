# UPS Watson Incident Intelligence: Graph Traversal

---

## The Problem with Flat Retrieval

The naive approach to querying a knowledge graph is to ignore the graph. You embed the user's question, find the N documents with the highest cosine similarity, and dump them into the LLM. The graph structure and edge types are stored but never used — every retrieved document is treated equally regardless of how it connects to anything else.

This breaks in two ways. First, you pull in documents that match the query's words but aren't structurally the right kind of information for what's being asked. Second, you miss documents that are one hop away and directly relevant, simply because they don't share vocabulary with the query.

---

## The Core Idea

Every edge in the graph already encodes something more than "these documents are related." The edge type tells you the *functional role* one document plays relative to another:

- `PRECEDED_BY` means there is a temporal/causal chain between them
- `PROVIDES_CONTEXT_FOR` means one document explains the background needed to understand the other
- `IMPLEMENTS` means one document is the action taken as a result of the other
- `REFERENCES` means one document explicitly cites the other
- `SUPPORTS` means one document corroborates the findings of the other
- `CONTRADICTS` means the documents present conflicting information
- `EXTENDS` means one document builds upon the other
- `SHARES_DOMAIN_WITH` means they cover the same domain without a direct relationship

This is deterministic information, not a probability estimate. If you know what kind of information a query needs, you know which edge types to follow first — without any fuzzy scoring.

---

## The Traversal System

### Step 1 — Classify the Query Intent

Before touching the graph, one LLM call classifies what kind of question is being asked. Four intents cover the meaningful question types for incident management:

| Intent | Example query | What it needs |
|--------|--------------|---------------|
| `causal` | "What caused the cert outage?" | The temporal and contextual chain leading to the incident |
| `resolution` | "How was the ODLM issue fixed?" | The action taken and what decision it came from |
| `timeline` | "What changed in the week before the November incident?" | Chronological ordering of related events |
| `similar` | "Has this kind of pod restart happened before?" | Corroborating cases and domain-shared incidents |

### Step 2 — Map Intent to Edge Type Priorities

Each intent maps deterministically to an ordered list of edge types. The ordering comes from what the edge types mean, not from learned weights:

| Intent | Priority order |
|--------|---------------|
| `causal` | `PRECEDED_BY` → `PROVIDES_CONTEXT_FOR` → `SUPPORTS` |
| `resolution` | `IMPLEMENTS` → `REFERENCES` → `PROVIDES_CONTEXT_FOR` |
| `timeline` | `PRECEDED_BY` (both directions) → `REFERENCES` |
| `similar` | `SUPPORTS` → `SHARES_DOMAIN_WITH` → `EXTENDS` |

Edges whose type does not appear in the priority list for the current intent are not followed.

### Step 3 — Best-First Traversal

Start at the seed document — the one with the highest embedding similarity to the query. From each node, collect all outgoing edges, rank them by their position in the intent's priority order, and follow the highest-ranked edge first.

This is best-first: you commit to a path, but you do so based on what the edge type tells you about what you will find at the next node. You do not expand in all directions simultaneously.

If the highest-priority edge at the current node has already been visited, move to the next edge in the priority order. Visited nodes are not revisited.

### Step 4 — Edge Description as Framing

Each edge carries a natural-language description generated during Phase 4 of the graph construction pipeline — a sentence explaining specifically why these two documents are related.

When a neighbor document is retrieved via a traversal hop, its raw text is not injected into the LLM context alone. The edge description is prepended as framing:

> *"This document provides context for the previous one because it describes the certificate renewal process that directly triggered the outage."*

The LLM now knows why it is reading this document and what role it plays in the answer — not just that it is somehow related.

### Step 5 — Stopping Conditions

Traversal stops when any of the following is true:

- The next available edge type is not in the current intent's priority list (the graph has no more structurally relevant hops)
- A maximum hop count is reached (default: 4)
- A maximum document count is reached (default: 5)

The result is a small, ordered, framed set of documents selected because they are structurally the right kind of information for the query — not because they happened to share vocabulary with it.

---

## Why This Works

The key property is that the edge type is a deterministic signal, not an estimate. `PRECEDED_BY` always means temporal causality. `IMPLEMENTS` always means an action taken from a decision. These meanings do not degrade or require calibration.

The only place uncertainty enters the system is in Step 1 — classifying the query intent. This is a single, explicit LLM call with a small output space (four categories). If it classifies incorrectly, the failure is visible and fixable at that one step rather than distributed across a scoring function.

Everything downstream of intent classification is deterministic graph traversal guided by edge semantics.

---

*Built for UPS Watson Incident Intelligence — June 2026*
