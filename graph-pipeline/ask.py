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
from typing import Any, Dict, List, Set

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from neo4j_handler import Neo4jHandler
from es_handler import ElasticsearchHandler
from llm_client import get_llm_client
import config

# ── Traversal parameters ──────────────────────────────────────────────────────

MAX_DOCS = 6   # maximum documents to collect (config.NUM_SEEDS anchors + graph hops)
MAX_HOPS = 4   # maximum graph edges followed beyond the seeds

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
    "thematic":   "a pattern, count, or trend across all incidents (corpus-wide)",
}

# The four traversal intents are single-incident-scoped and drive best-first
# traversal. 'thematic' is corpus-wide (aggregation / "across all incidents") and
# is NOT a traversal at all — it routes to run_thematic(), which runs a graph-wide
# Cypher aggregation or gathers a full theme-set, because a best-first walk capped
# at MAX_DOCS structurally under-samples the corpus on counting/pattern questions.
VALID_INTENTS = set(EDGE_PRIORITIES) | {"thematic"}

# ── Thematic (corpus-wide) parameters ─────────────────────────────────────────
THEMATIC_TOP_N = 12        # entities returned by an entity_count aggregation
THEMATIC_MAX_DOCS = 40     # documents gathered for a theme_synthesis (no walk cap)
THEMATIC_DOC_CHARS = 6000  # per-document char cap when building synthesis context

# Strong corpus-wide markers. These only PROMOTE an otherwise single-incident
# classification to 'thematic' (never the reverse), as a backstop for the case the
# LLM rounds an aggregation question to the nearest single-incident intent.
THEMATIC_MARKERS = (
    "how often", "how many", "across all", "most common", "most frequent",
    "appear most", "appears most", "which incidents", "which teams",
    "which components", "recurring", "on average", "over time", "what pattern",
    "patterns", "trend", "in total", "number of incidents", "most involved",
)


def _looks_thematic(query: str) -> bool:
    """True if the query carries a strong corpus-wide aggregation marker."""
    q = query.lower()
    return any(marker in q for marker in THEMATIC_MARKERS)


class KnowledgeGraphQuerier:
    """Query the knowledge graph using intent-driven best-first traversal."""

    def __init__(self):
        self.neo4j = Neo4jHandler()
        self.llm = get_llm_client()
        self.es = ElasticsearchHandler() if config.ES_ENABLED else None

    # ── Step 1: Intent classification ────────────────────────────────────────

    def classify_intent(self, query: str) -> str:
        """Classify the query into one of five intents via one LLM call.

        Four are single-incident-scoped (causal/resolution/timeline/similar); the
        fifth, 'thematic', is corpus-wide. A keyword backstop only ever promotes a
        single-incident verdict to thematic, never the reverse — so an aggregation
        question the LLM mislabels as 'causal' still gets routed correctly.
        """
        prompt = f"""Classify this incident management query into exactly one category.

Categories:
- causal: what caused a SPECIFIC incident, failure, or outage — root cause of one event
- resolution: how a SPECIFIC issue was fixed, what steps resolved it, the solution
- timeline: the sequence of events for a SPECIFIC incident — what changed when
- similar: whether a SPECIFIC incident happened before / find related past cases
- thematic: a question ABOUT THE WHOLE CORPUS rather than one incident — counting,
  ranking, trends, or patterns across many incidents. Examples: "how often was X a
  contributing factor", "which teams appear most", "what is the most common root
  cause", "which incidents were certificate-related", "what recurring patterns exist".

Query: "{query}"

Return ONLY valid JSON: {{"intent": "causal" | "resolution" | "timeline" | "similar" | "thematic"}}"""

        try:
            result = self.llm.generate_json(prompt)
            intent = result.get("intent", "").lower().strip()
            if intent not in VALID_INTENTS:
                intent = "similar"
        except Exception:
            intent = "similar"

        if intent != "thematic" and _looks_thematic(query):
            intent = "thematic"
        return intent

    # ── Step 2: Seed document selection ──────────────────────────────────────

    def find_seeds(self, query: str) -> List[Dict[str, Any]]:
        """Return the top NUM_SEEDS starting documents for traversal.

        Primary path is Elasticsearch hybrid retrieval (BM25 + dense kNN fused
        with RRF). Using several seeds instead of one makes traversal robust to
        a single bad seed. Falls back to a single TF-IDF seed if ES is disabled,
        unreachable, or returns nothing.
        """
        if self.es is not None:
            try:
                query_vector = None
                if config.ES_USE_DENSE:
                    query_vector = self.llm.embed([query])[0]

                seeds = self.es.hybrid_search(query, query_vector, size=config.NUM_SEEDS)
                if seeds:
                    return seeds
                print("  ES returned no hits — falling back to TF-IDF seed.")
            except Exception as e:
                print(f"  ES seed retrieval failed ({e}); falling back to TF-IDF seed.")

        return [self._find_seed_tfidf(query)]

    def _find_seed_tfidf(self, query: str) -> Dict[str, Any]:
        """Fallback: single best seed via TF-IDF over document topics and entities."""
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
        best = all_docs[best_idx]
        return {"hash": best["hash"], "filepath": best["filepath"]}

    # ── Step 3: Graph traversal ───────────────────────────────────────────────

    def get_neighbors(
        self, doc_hash: str, edge_types: List[str], visited: Set[str]
    ) -> List[Dict[str, Any]]:
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
        results: Dict[str, Dict[str, Any]] = {}

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

    def traverse(self, seeds: List[Dict[str, Any]], intent: str) -> List[Dict[str, Any]]:
        """Best-first traversal from one or more seed documents.

        All retrieved seeds become traversal anchors. A single priority heap
        spans every visited node; at each step the globally best available edge
        (by intent priority rank, then strength) is popped and followed to an
        unvisited neighbor — so a strong edge discovered from any anchor competes
        with edges from every other. Continues until MAX_DOCS documents are
        collected or no qualifying edges remain.

        Returns an ordered list of dicts: {hash, filepath, edge_description}.
        edge_description is None for seeds (they were retrieved, not followed).
        """
        edge_types = EDGE_PRIORITIES.get(intent, [])
        priority_rank = {etype: i for i, etype in enumerate(edge_types)}

        visited: Set[str] = set()
        path: List[Dict[str, Any]] = []

        # Seeds anchor the path in retrieval-rank order, deduplicated.
        for seed in seeds:
            if seed["hash"] in visited:
                continue
            visited.add(seed["hash"])
            path.append(
                {
                    "hash": seed["hash"],
                    "filepath": seed["filepath"],
                    "edge_description": None,
                }
            )
            if len(path) >= MAX_DOCS:
                return path

        # Heap entries: (rank, -strength, counter, neighbor_dict)
        # counter breaks ties without comparing dicts
        heap: list = []
        counter = 0

        # Seed the frontier with the graph neighbors of every anchor.
        for anchor in path:
            for neighbor in self.get_neighbors(anchor["hash"], edge_types, visited):
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

    def build_context(self, path: List[Dict[str, Any]]) -> str:
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

        # Generous budget: a reasoning model spends tokens on its hidden
        # reasoning channel before the grounded answer, so leave headroom.
        return self.llm.generate_text(prompt, max_tokens=4096)

    # ── Thematic (corpus-wide) path ───────────────────────────────────────────
    # Aggregation/pattern questions are NOT traversals. Best-first walking from a
    # few seeds with a MAX_DOCS cap systematically under-samples the corpus on
    # "across all incidents" questions. Instead we either run an exact graph-wide
    # Cypher count (entity_count) or gather the full theme-matching set and let the
    # LLM tally/pattern over ALL of it (theme_synthesis).

    def _available_entity_types(self) -> List[str]:
        """Distinct entity_type labels present in the graph (for count scoping)."""
        try:
            rows = self.neo4j.query_graph(
                "MATCH (e:Entity) WHERE e.entity_type IS NOT NULL "
                "RETURN DISTINCT e.entity_type AS t ORDER BY t"
            )
            return [r["t"] for r in rows if r.get("t")]
        except Exception:
            return []

    def plan_thematic(self, query: str) -> Dict[str, Any]:
        """Decide HOW to answer a corpus-wide question (one LLM call).

        Returns {mode, entity_type, theme}:
          - 'entity_count'    → exact Cypher GROUP BY/COUNT over MENTIONS edges,
            optionally scoped to one entity_type (e.g. "which teams appear most").
          - 'theme_synthesis' → gather every document matching a theme (or all
            incidents) and let the LLM aggregate over the whole set, for things that
            are not stored entities (e.g. contributing factors, root-cause families).
        """
        types = self._available_entity_types()
        types_str = ", ".join(types) if types else "(none available)"
        prompt = f"""A user asked a corpus-wide question about a knowledge graph of incident reports (RCAs) and change requests (CRs).

Question: "{query}"

Entity types stored as nodes in the graph: {types_str}

Choose ONE mode:
- "entity_count": the answer is a COUNT or RANKING of a kind of thing the graph stores
  as entity nodes (teams, components, services, technologies...). Set entity_type to the
  best-matching type from the list above, or null to count across all entities.
- "theme_synthesis": the answer needs reading across many documents to find a pattern,
  tally something that is NOT a stored entity (e.g. contributing factors, root-cause
  categories), or list which incidents match a theme. Set theme to a short lowercase
  keyword to filter documents by (e.g. "certificate", "communication", "pod restart"),
  or null to consider all incidents.

Return ONLY valid JSON:
{{"mode": "entity_count" | "theme_synthesis", "entity_type": "<type or null>", "theme": "<keyword or null>"}}"""

        try:
            plan = self.llm.generate_json(prompt)
        except Exception:
            plan = {}

        mode = plan.get("mode")
        if mode not in ("entity_count", "theme_synthesis"):
            mode = "theme_synthesis"

        def _clean(v):
            if isinstance(v, str) and v.strip().lower() in ("null", "none", ""):
                return None
            return v

        return {
            "mode": mode,
            "entity_type": _clean(plan.get("entity_type")),
            "theme": _clean(plan.get("theme")),
        }

    def aggregate_entities(
        self, entity_type: Any, top_n: int = THEMATIC_TOP_N
    ) -> List[Dict[str, Any]]:
        """Exact corpus-wide count of documents per entity via MENTIONS edges."""
        return self.neo4j.query_graph(
            """
            MATCH (d:Document)-[:MENTIONS]->(e:Entity)
            WHERE $etype IS NULL OR toLower(e.entity_type) = toLower($etype)
            RETURN e.name AS name, e.entity_type AS type,
                   count(DISTINCT d) AS doc_count,
                   collect(DISTINCT d.filepath)[..8] AS docs
            ORDER BY doc_count DESC, name ASC
            LIMIT $top_n
            """,
            etype=entity_type,
            top_n=top_n,
        )

    def gather_thematic_set(
        self, theme: Any, limit: int = THEMATIC_MAX_DOCS
    ) -> List[str]:
        """Collect the FULL set of documents for a theme — no best-first cap.

        With a theme: match it against each document's topics, entities, mentioned
        entity names, and filepath. Without one: fall back to all incident reports
        (RCA/*), since corpus-wide pattern questions are about incidents.
        """
        if theme:
            rows = self.neo4j.query_graph(
                """
                MATCH (d:Document)
                OPTIONAL MATCH (d)-[:MENTIONS]->(e:Entity)
                WITH d, collect(toLower(e.name)) AS enames
                WHERE any(t IN d.topics   WHERE toLower(t) CONTAINS $theme)
                   OR any(x IN d.entities WHERE toLower(x) CONTAINS $theme)
                   OR any(n IN enames     WHERE n CONTAINS $theme)
                   OR toLower(d.filepath) CONTAINS $theme
                RETURN d.filepath AS filepath
                ORDER BY d.filepath
                LIMIT $limit
                """,
                theme=theme.lower(),
                limit=limit,
            )
        else:
            rows = self.neo4j.query_graph(
                """
                MATCH (d:Document)
                WHERE toLower(d.filepath) STARTS WITH 'rca/'
                RETURN d.filepath AS filepath
                ORDER BY d.filepath
                LIMIT $limit
                """,
                limit=limit,
            )
        return [r["filepath"] for r in rows]

    def _build_thematic_context(self, filepaths: List[str]) -> str:
        """Concatenate the gathered documents (each truncated) for synthesis."""
        sections = []
        for i, fp in enumerate(filepaths):
            try:
                text = (config.DOCUMENTS_DIR / fp).read_text(encoding="utf-8")
            except Exception:
                continue
            if len(text) > THEMATIC_DOC_CHARS:
                text = text[:THEMATIC_DOC_CHARS] + "\n…[truncated]"
            sections.append(f"[Document {i + 1}: {fp}]\n\n{text}")
        return ("\n\n" + "─" * 60 + "\n\n").join(sections)

    def answer_entity_count(
        self, query: str, entity_type: Any, counts: List[Dict[str, Any]]
    ) -> str:
        """Phrase an answer from EXACT counts (the LLM never does the counting)."""
        table = "\n".join(
            f"- {r['name']} ({r['type']}): mentioned in {r['doc_count']} documents"
            for r in counts
        )
        scope = f" (entity type: {entity_type})" if entity_type else ""
        prompt = f"""You are answering a corpus-wide question using EXACT counts already computed from a knowledge graph of incident and change-request documents.

Question: {query}

Exact document-mention counts{scope}, highest first:
{table}

Write a clear, direct answer grounded in these exact numbers, naming the top entities and their counts. Do not invent or recompute any numbers beyond those given."""
        return self.llm.generate_text(prompt, max_tokens=1024)

    def answer_theme_synthesis(
        self, query: str, theme: Any, context: str, filepaths: List[str]
    ) -> str:
        """Aggregate/pattern over the FULL gathered set in one LLM call."""
        if not context.strip():
            suffix = f" for the theme '{theme}'." if theme else "."
            return (
                "No documents in the knowledge graph matched this question" + suffix
            )
        scope = f"documents related to '{theme}'" if theme else "all incident reports"
        prompt = f"""You are answering a corpus-wide question by reading across {scope}. You have the COMPLETE set of matching documents ({len(filepaths)} total), so you can count and find patterns across all of them.

Question: {query}

Documents:
{context}

Instructions:
- Aggregate across ALL the documents above — count occurrences, rank, or describe the recurring pattern as the question asks.
- Be specific: cite document names and give counts (e.g. "X of {len(filepaths)} documents…") where relevant.
- Base every claim only on the documents provided. If they don't support a confident answer, say so.

Answer:"""
        return self.llm.generate_text(prompt, max_tokens=4096)

    def run_thematic(self, query: str) -> Dict[str, Any]:
        """Answer a corpus-wide question via aggregation, not traversal."""
        plan = self.plan_thematic(query)

        if plan["mode"] == "entity_count":
            counts = self.aggregate_entities(plan["entity_type"])
            if counts:
                answer = self.answer_entity_count(query, plan["entity_type"], counts)
                seen: Set[str] = set()
                path: List[Dict[str, Any]] = []
                for row in counts:
                    for fp in row.get("docs", []):
                        if fp not in seen:
                            seen.add(fp)
                            path.append({
                                "filepath": fp,
                                "edge_description": f"mentions {row['name']}",
                                "is_seed": False,
                            })
                return {
                    "query": query,
                    "intent": "thematic",
                    "intent_description": INTENT_DESCRIPTIONS["thematic"],
                    "edge_priority": [],
                    "seeds": [],
                    "thematic_plan": plan,
                    "aggregation": counts,
                    "path": path,
                    "answer": answer,
                }
            # No entities of that type — fall back to reading by theme.
            plan = {"mode": "theme_synthesis", "entity_type": None,
                    "theme": plan.get("entity_type")}

        filepaths = self.gather_thematic_set(plan.get("theme"))
        context = self._build_thematic_context(filepaths)
        answer = self.answer_theme_synthesis(query, plan.get("theme"), context, filepaths)
        label = (f"matched theme '{plan['theme']}'" if plan.get("theme")
                 else "incident document (corpus-wide analysis)")
        path = [
            {"filepath": fp, "edge_description": label, "is_seed": False}
            for fp in filepaths
        ]
        return {
            "query": query,
            "intent": "thematic",
            "intent_description": INTENT_DESCRIPTIONS["thematic"],
            "edge_priority": [],
            "seeds": [],
            "thematic_plan": plan,
            "aggregation": None,
            "path": path,
            "answer": answer,
        }

    # ── Main entry point ──────────────────────────────────────────────────────

    def run_query(self, query: str) -> Dict[str, Any]:
        """Run the full pipeline and return structured results (no printing).

        Shared by the CLI (`ask`) and the web UI (`chat_app.py`). Returns the
        intent, the edge plan, the seed documents, the traversal path (each hop
        labeled with the edge description that justified it), and the answer.

        Thematic (corpus-wide) questions branch to run_thematic(), which answers by
        aggregation/full-set synthesis instead of best-first traversal.
        """
        intent = self.classify_intent(query)
        if intent == "thematic":
            return self.run_thematic(query)

        seeds = self.find_seeds(query)
        path = self.traverse(seeds, intent)
        context = self.build_context(path)
        answer = self.generate_answer(query, intent, context)

        return {
            "query": query,
            "intent": intent,
            "intent_description": INTENT_DESCRIPTIONS.get(intent, ""),
            "edge_priority": EDGE_PRIORITIES.get(intent, []),
            "seeds": [s["filepath"] for s in seeds],
            "path": [
                {
                    "filepath": node["filepath"],
                    "edge_description": node.get("edge_description"),
                    "is_seed": node.get("edge_description") is None,
                }
                for node in path
            ],
            "answer": answer,
        }

    def ask(self, query: str) -> str:
        """Run a query and print a human-readable trace (CLI entry point)."""
        print(f"\nQuery: {query}")
        result = self.run_query(query)

        if result["intent"] == "thematic":
            plan = result.get("thematic_plan", {})
            print(f"Intent: thematic ({result['intent_description']})")
            print(f"Plan: mode={plan.get('mode')} "
                  f"entity_type={plan.get('entity_type')} theme={plan.get('theme')}")
            if result.get("aggregation"):
                print("\nTop entities (exact corpus-wide counts):")
                for r in result["aggregation"]:
                    print(f"  {r['doc_count']:>3}  {r['name']} ({r['type']})")
            else:
                print(f"\nDocuments analysed ({len(result['path'])}):")
                for node in result["path"]:
                    print(f"  - {node['filepath']}")
            print("\nAnswer:\n")
            return result["answer"]

        print(f"Intent: {result['intent']} ({result['intent_description']})")
        print(f"Edge priority: {' → '.join(result['edge_priority'])}")

        print(f"\nSeed documents ({len(result['seeds'])}):")
        for filepath in result["seeds"]:
            print(f"  - {filepath}")

        print(f"\nTraversal path ({len(result['path'])} documents):")
        for i, node in enumerate(result["path"]):
            prefix = "seed" if node["is_seed"] else f"hop {i}"
            print(f"  {prefix}: {node['filepath']}")

        print("\nAnswer:\n")
        return result["answer"]

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
