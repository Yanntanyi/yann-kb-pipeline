"""ask.py — Query the knowledge graph using intent-driven best-first traversal.

How it works:
  1. Classify the query into one of four intents (causal, resolution, timeline, similar)
  2. Find the best seed document using TF-IDF over document topics and entities
  3. Traverse the graph best-first, following edges in the priority order for that intent
  4. Prepend each retrieved document with the edge description that explains why it was followed
  5. Generate a grounded answer from the ordered, framed context

Usage:
  python ask.py "What caused the CPD certificate outage?"
  python ask.py                          (interactive mode)
  python ask.py --from-phase 5 (not relevant here — this is a query tool, not pipeline)
"""

import heapq
import sys
import requests
from typing import Dict, List, Optional, Set

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from neo4j_handler import Neo4jHandler
from llm_client import LMStudioClient
import config

# ── Traversal parameters ──────────────────────────────────────────────────────

MAX_DOCS = 5   # maximum documents to collect before stopping
MAX_HOPS = 4   # maximum hops from the seed document

# Ordered edge type priorities per query intent.
# Index in the list = priority rank (lower index = follow first).
# Edge types not in the list for a given intent are not followed.
EDGE_PRIORITIES: Dict[str, List[str]] = {
    "causal":     ["PRECEDED_BY", "PROVIDES_CONTEXT_FOR", "SUPPORTS"],
    "resolution": ["IMPLEMENTS", "REFERENCES", "PROVIDES_CONTEXT_FOR"],
    "timeline":   ["PRECEDED_BY", "REFERENCES"],
    "similar":    ["SUPPORTS", "SHARES_DOMAIN_WITH", "EXTENDS"],
}

INTENT_DESCRIPTIONS = {
    "causal":     "what caused an incident or failure",
    "resolution": "how something was fixed or resolved",
    "timeline":   "the sequence of events and what changed when",
    "similar":    "whether similar incidents occurred before",
}


class KnowledgeGraphQuerier:
    """Query the knowledge graph using intent-driven best-first traversal."""

    def __init__(self):
        self.neo4j = Neo4jHandler()
        self.llm = LMStudioClient()

    # ── Step 1: Intent classification ────────────────────────────────────────

    def classify_intent(self, query: str) -> str:
        """Classify the query into one of four traversal intents via one LLM call."""
        prompt = f"""Classify this incident management query into exactly one category.

Categories:
- causal: asking what caused an incident, failure, or outage — root cause questions
- resolution: asking how something was fixed, what steps resolved it, or what the solution was
- timeline: asking what happened before or after an event, chronological questions, what changed when
- similar: asking whether this happened before, finding related past incidents, pattern matching

Query: "{query}"

Return ONLY valid JSON: {{"intent": "causal" | "resolution" | "timeline" | "similar"}}"""

        try:
            result = self.llm.generate_json(prompt)
            intent = result.get("intent", "").lower().strip()
            if intent not in EDGE_PRIORITIES:
                intent = "similar"
            return intent
        except Exception:
            return "similar"

    # ── Step 2: Seed document selection ──────────────────────────────────────

    def find_seed(self, query: str) -> Dict:
        """Find the best starting document using TF-IDF over document topics and entities."""
        all_docs = self.neo4j.query_graph(
            "MATCH (d:Document) "
            "RETURN d.hash as hash, d.filepath as filepath, "
            "d.topics as topics, d.entities as entities"
        )

        if not all_docs:
            raise RuntimeError(
                "No documents found in Neo4j. Run the pipeline (main.py) first."
            )

        doc_texts = []
        for doc in all_docs:
            topics = doc.get("topics") or []
            entities = doc.get("entities") or []
            doc_texts.append(" ".join(topics + entities))

        # Fit on docs + query together so they share the same vocabulary
        vectorizer = TfidfVectorizer(stop_words="english", min_df=1)
        all_texts = doc_texts + [query]
        tfidf_matrix = vectorizer.fit_transform(all_texts)

        query_vec = tfidf_matrix[-1]
        doc_vecs = tfidf_matrix[:-1]
        similarities = cosine_similarity(query_vec, doc_vecs)[0]

        best_idx = int(np.argmax(similarities))
        return all_docs[best_idx]

    # ── Step 3: Graph traversal ───────────────────────────────────────────────

    def get_neighbors(
        self, doc_hash: str, edge_types: List[str], visited: Set[str]
    ) -> List[Dict]:
        """Return all unvisited Document neighbors reachable via the given edge types.

        Checks both outgoing (current→neighbor) and incoming (neighbor→current)
        edges. Both directions are relevant — e.g. for a causal query at the RCA
        node, the PRECEDED_BY edge points inward (CR→RCA), so we need incoming.
        Deduplicates by neighbor hash, keeping the stronger edge when both
        directions exist.
        """
        if not edge_types:
            return []

        visited_list = list(visited)
        results: Dict[str, Dict] = {}

        outgoing = self.neo4j.query_graph(
            """
            MATCH (d:Document {hash: $hash})-[r]->(neighbor:Document)
            WHERE type(r) IN $types AND NOT neighbor.hash IN $visited
            RETURN type(r) AS rel_type,
                   r.description AS description,
                   coalesce(r.strength, 5) AS strength,
                   neighbor.hash AS neighbor_hash,
                   neighbor.filepath AS filepath
            """,
            hash=doc_hash,
            types=edge_types,
            visited=visited_list,
        )

        incoming = self.neo4j.query_graph(
            """
            MATCH (neighbor:Document)-[r]->(d:Document {hash: $hash})
            WHERE type(r) IN $types AND NOT neighbor.hash IN $visited
            RETURN type(r) AS rel_type,
                   r.description AS description,
                   coalesce(r.strength, 5) AS strength,
                   neighbor.hash AS neighbor_hash,
                   neighbor.filepath AS filepath
            """,
            hash=doc_hash,
            types=edge_types,
            visited=visited_list,
        )

        for row in outgoing + incoming:
            key = row["neighbor_hash"]
            if key not in results or row["strength"] > results[key]["strength"]:
                results[key] = row

        return list(results.values())

    def traverse(self, seed: Dict, intent: str) -> List[Dict]:
        """Best-first traversal from the seed document.

        Maintains a priority heap across all visited nodes. At each step, pops
        the globally best available edge (by intent priority rank, then strength)
        and follows it to an unvisited neighbor. Continues until MAX_DOCS
        documents are collected or no qualifying edges remain.

        Returns an ordered list of dicts: {hash, filepath, edge_description}.
        edge_description is None for the seed (no edge led there).
        """
        edge_types = EDGE_PRIORITIES.get(intent, [])
        priority_rank = {etype: i for i, etype in enumerate(edge_types)}

        visited: Set[str] = {seed["hash"]}
        path = [
            {
                "hash": seed["hash"],
                "filepath": seed["filepath"],
                "edge_description": None,
            }
        ]

        # Heap entries: (rank, -strength, counter, neighbor_dict)
        # counter breaks ties without comparing dicts
        heap: list = []
        counter = 0

        for neighbor in self.get_neighbors(seed["hash"], edge_types, visited):
            rank = priority_rank.get(neighbor["rel_type"], 999)
            heapq.heappush(heap, (rank, -neighbor["strength"], counter, neighbor))
            counter += 1

        hops = 0
        while heap and len(path) < MAX_DOCS and hops < MAX_HOPS:
            rank, _, _, neighbor = heapq.heappop(heap)

            if neighbor["neighbor_hash"] in visited:
                continue

            visited.add(neighbor["neighbor_hash"])
            path.append(
                {
                    "hash": neighbor["neighbor_hash"],
                    "filepath": neighbor["filepath"],
                    "edge_description": neighbor["description"],
                }
            )
            hops += 1

            for next_neighbor in self.get_neighbors(
                neighbor["neighbor_hash"], edge_types, visited
            ):
                next_rank = priority_rank.get(next_neighbor["rel_type"], 999)
                heapq.heappush(
                    heap, (next_rank, -next_neighbor["strength"], counter, next_neighbor)
                )
                counter += 1

        return path

    # ── Step 4: Context building ──────────────────────────────────────────────

    def build_context(self, path: List[Dict]) -> str:
        """Read each document in the traversal path and prepend its edge framing."""
        sections = []

        for i, node in enumerate(path):
            try:
                content = (config.DOCUMENTS_DIR / node["filepath"]).read_text(
                    encoding="utf-8"
                )
            except Exception as e:
                print(f"  Warning: could not read {node['filepath']}: {e}")
                continue

            header = f"[Document {i + 1}: {node['filepath']}]"
            edge_desc = node.get("edge_description")

            if edge_desc:
                framing = f"[Why this document is here: {edge_desc}]"
                sections.append(f"{header}\n{framing}\n\n{content}")
            else:
                sections.append(f"{header}\n\n{content}")

        return ("\n\n" + "─" * 60 + "\n\n").join(sections)

    # ── Step 5: Answer generation ─────────────────────────────────────────────

    def generate_answer(self, query: str, intent: str, context: str) -> str:
        """Generate a grounded answer from the ordered, framed context."""
        intent_desc = INTENT_DESCRIPTIONS.get(intent, "the answer")

        prompt = f"""You are an expert on cloud infrastructure incidents. Answer the following question using ONLY the documents provided below.

The question is asking about {intent_desc}.

Question: {query}

Documents (in traversal order — each is labeled with why it was retrieved):
{context}

Instructions:
- Answer specifically and directly using the information in the documents
- Reference specific document names, dates, components, and technical details
- If the documents don't contain enough information to fully answer, say so explicitly
- Do not introduce information that is not in the provided documents

Answer:"""

        response = requests.post(
            f"{config.LM_STUDIO_BASE_URL}/chat/completions",
            json={
                "model": config.LM_STUDIO_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    # ── Main entry point ──────────────────────────────────────────────────────

    def ask(self, query: str) -> str:
        """Run the full pipeline: classify → seed → traverse → frame → answer."""
        print(f"\nQuery: {query}")

        # Step 1: Intent
        intent = self.classify_intent(query)
        print(f"Intent: {intent} ({INTENT_DESCRIPTIONS[intent]})")
        print(f"Edge priority: {' → '.join(EDGE_PRIORITIES[intent])}")

        # Step 2: Seed
        seed = self.find_seed(query)
        print(f"\nSeed document: {seed['filepath']}")

        # Step 3: Traverse
        path = self.traverse(seed, intent)
        print(f"\nTraversal path ({len(path)} documents):")
        for i, node in enumerate(path):
            prefix = "  seed" if i == 0 else f"  hop {i}"
            print(f"  {prefix}: {node['filepath']}")

        # Step 4: Context
        context = self.build_context(path)

        # Step 5: Answer
        print("\nGenerating answer...\n")
        return self.generate_answer(query, intent, context)

    def close(self):
        self.neo4j.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    querier = KnowledgeGraphQuerier()
    try:
        if len(sys.argv) > 1:
            query = " ".join(sys.argv[1:])
            print(querier.ask(query))
        else:
            print("UPS Watson Knowledge Graph\n")
            print("Ask a question about incidents, changes, or root causes.")
            print("Type 'quit' to exit.\n")
            while True:
                try:
                    query = input("Question: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not query:
                    continue
                if query.lower() in ("quit", "exit", "q"):
                    break
                print(querier.ask(query))
                print()
    finally:
        querier.close()
