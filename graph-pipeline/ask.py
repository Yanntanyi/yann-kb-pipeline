"""ask.py — Query the knowledge graph using intent-driven best-first traversal.

How it works:
  1. Classify the query into one of four intents (causal, resolution, timeline, similar)
  2. Find the best seed document using TF-IDF over document topics and entities
  3. Traverse the graph best-first, following edges in the priority order for that intent
  4. Prepend each retrieved document with the edge description that explains why it was followed
  5. Generate a grounded answer from the ordered, framed context

Usage:
  python3 ask.py "What caused the CPD certificate outage?"
  python3 ask.py                          (interactive mode)
  python3 ask.py --from-phase 5 (not relevant here — this is a query tool, not pipeline)
"""

import heapq
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Set

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from neo4j_handler import Neo4jHandler
from es_handler import ElasticsearchHandler
from llm_client import get_llm_client
import config


@contextmanager
def _timed(store: Dict[str, float], label: str):
    """Record wall-clock seconds for the wrapped block into store[label]."""
    start = time.perf_counter()
    try:
        yield
    finally:
        store[label] = time.perf_counter() - start


def _format_timing(timing: Dict[str, float]) -> str:
    """One-line 'total Xs · stage Ys · …' summary for the CLI/trace."""
    if not timing:
        return ""
    parts = [f"{k} {v}s" for k, v in timing.items() if k != "total"]
    head = f"total {timing.get('total', '?')}s"
    return head + ("  ·  " + "  ·  ".join(parts) if parts else "")

# ── Traversal parameters ──────────────────────────────────────────────────────

MAX_DOCS = 10  # (legacy best-first walk) maximum documents to collect
MAX_HOPS = 7   # (legacy best-first walk) maximum graph edges followed beyond seeds

# ── Augment-not-replace retrieval (the four traversal intents) ────────────────
# The graph AUGMENTS search instead of competing with it for a fixed doc budget —
# which is what dissolves the goal-blind walk. We keep the full RAG top-K as the
# base (never evicted, so retrieval can't do worse than flat RAG), then ADD a few
# graph bridges: documents 1 hop from the base via the intent's typed edges, each
# carrying the edge description that justifies it (the audit trail).
AUGMENT_BASE_K = 10  # RAG top-K base — always kept (matches flat-RAG TOP_K)
MAX_BRIDGES = 5      # graph bridges added on top of the base
# Balance note: keep NUM_SEEDS (config.py) well below MAX_DOCS so the walk has room
# to traverse. With NUM_SEEDS=3 and these values the walk can take up to 7 hops; if
# NUM_SEEDS is near MAX_DOCS (e.g. 5/6) the seeds eat the budget and traversal barely
# happens. Raising MAX_DOCS adds reach but more context/tokens — dial back to ~8 if
# answers lose focus.

# Ordered edge type priorities per query intent.
# Index in the list = priority rank (lower index = follow first).
# Edge types not in the list for a given intent are not followed.
EDGE_PRIORITIES: Dict[str, List[str]] = {
    "causal":     ["CAUSED_BY", "PRECEDED_BY", "PROVIDES_CONTEXT_FOR"],
    "resolution": ["REMEDIATED_BY", "REFERENCES", "PROVIDES_CONTEXT_FOR"],
    "timeline":   ["PRECEDED_BY", "CAUSED_BY", "REFERENCES"],
    "similar":    ["RECURRENCE_OF", "PROVIDES_CONTEXT_FOR"],
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
# theme_synthesis reads the matching set HYBRID: the most query-relevant documents
# in FULL TEXT (depth — so it can state specific facts, contributing factors,
# resolution detail), and the rest as compact graph fingerprints (breadth — so it
# can still count and find patterns across the whole set). Pure fingerprints lost to
# flat RAG on "state this specific fact" questions; full text for everything is too
# many tokens. The hybrid gets both, and the corpus is small enough that it's cheap.
THEMATIC_TOP_N = 12        # entities returned by an entity_count aggregation
THEMATIC_MAX_DOCS = 50     # documents in the matching set for a theme_synthesis
THEMATIC_FULLTEXT_N = 12   # of those, how many (most query-relevant) to read in FULL
                           # TEXT; the remainder are read as compact fingerprints
THEMATIC_ANSWER_TOKENS = 4096  # max_tokens is a CEILING, not a target: the prompt
# still asks for a short list/count, so answers stay concise. gpt-oss is a reasoning
# model whose hidden "thinking" tokens count against max_tokens, so a tight budget can
# be wholly consumed by reasoning and return empty content. The headroom feeds the
# reasoning scratchpad, not the visible answer.

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
        self._entity_types_cache = None  # filled lazily, reused across queries

    # ── Step 1: Intent classification ────────────────────────────────────────

    def classify(self, query: str) -> Dict[str, Any]:
        """One LLM call: pick the intent AND, if thematic, plan how to answer it.

        Folding the thematic plan into the classification call means a corpus-wide
        question costs the same TWO LLM calls as a traversal query (classify+plan,
        then answer) instead of three. The plan fields are ignored for the four
        single-incident intents.

        Returns {"intent": str, "thematic_plan": {...} | None}. A keyword backstop
        only ever promotes a single-incident verdict to thematic, never the reverse.
        """
        types = self._available_entity_types()
        types_str = ", ".join(types) if types else "(none)"
        prompt = f"""Classify this incident-management query, and if it is corpus-wide, plan how to answer it.

Single-incident intents (about ONE incident):
- causal: what caused a specific incident
- resolution: how a specific issue was fixed
- timeline: the sequence of events for a specific incident
- similar: whether a specific incident happened before / related past cases

Corpus-wide intent (about MANY incidents at once):
- thematic: counting, ranking, trends, or patterns across the whole corpus —
  "how often was X a factor", "which teams appear most", "most common root cause",
  "which incidents were certificate-related", "what recurring patterns exist".

If AND ONLY IF intent is "thematic", also fill a plan:
- mode "entity_count": a count/ranking of a stored entity kind. Set entity_type to the
  best match from these types: {types_str} (or null to count all entities).
- mode "theme_synthesis": needs reading across documents (patterns, contributing
  factors, which incidents match a theme). Set theme to a short lowercase keyword
  (e.g. "certificate", "communication") or null to consider all incidents.

Query: "{query}"

Return ONLY valid JSON:
{{"intent": "causal|resolution|timeline|similar|thematic", "mode": "entity_count|theme_synthesis|null", "entity_type": "<type or null>", "theme": "<keyword or null>"}}"""

        try:
            result = self.llm.generate_json(prompt)
            intent = str(result.get("intent", "")).lower().strip()
            if intent not in VALID_INTENTS:
                intent = "similar"
        except Exception:
            result, intent = {}, "similar"

        if intent != "thematic" and _looks_thematic(query):
            intent = "thematic"

        plan = self._normalize_plan(result) if intent == "thematic" else None
        return {"intent": intent, "thematic_plan": plan}

    def classify_intent(self, query: str) -> str:
        """Back-compat wrapper: return just the intent string."""
        return self.classify(query)["intent"]

    @staticmethod
    def _normalize_plan(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Clean a raw thematic plan (mode/entity_type/theme), coercing 'null' strings."""
        def _clean(v):
            if isinstance(v, str) and v.strip().lower() in ("null", "none", ""):
                return None
            return v

        mode = raw.get("mode")
        if mode not in ("entity_count", "theme_synthesis"):
            mode = "theme_synthesis"
        return {
            "mode": mode,
            "entity_type": _clean(raw.get("entity_type")),
            "theme": _clean(raw.get("theme")),
        }

    # ── Step 2: Seed document selection ──────────────────────────────────────

    def find_seeds(self, query: str, size: int = None) -> List[Dict[str, Any]]:
        """Return the top `size` documents (default NUM_SEEDS) by hybrid retrieval.

        Primary path is Elasticsearch hybrid retrieval (BM25 + dense kNN fused
        with RRF). For augment-not-replace this returns the full RAG top-K base;
        for the legacy walk it returns a few seeds. Falls back to a single TF-IDF
        seed if ES is disabled, unreachable, or returns nothing.
        """
        size = size or config.NUM_SEEDS
        if self.es is not None:
            try:
                query_vector = None
                if config.ES_USE_DENSE:
                    query_vector = self.llm.embed([query])[0]

                seeds = self.es.hybrid_search(query, query_vector, size=size)
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
        self, doc_hash: str, visited: Set[str]
    ) -> List[Dict[str, Any]]:
        """Return all unvisited Document neighbors reachable via ANY relationship edge.

        We fetch every Document→Document relationship edge (both directions),
        regardless of type. The intent's edge plan is applied later as a PRIORITY
        ORDER, not a hard filter — so the walk prefers on-plan edges but can still
        fall back to any relationship edge to reach a connected document. (Diagnostic
        showed the connecting edges exist but were being excluded as "off-plan".)

        MENTIONS edges point to Entity nodes, so the Document-neighbor match
        naturally excludes them. Both directions matter — e.g. for a causal query at
        the RCA node, the PRECEDED_BY edge points inward (CR→RCA). Deduplicates by
        neighbor hash, keeping the stronger edge when both directions exist.
        """
        visited_list = list(visited)
        results: Dict[str, Dict[str, Any]] = {}

        outgoing = self.neo4j.query_graph(
            """
            MATCH (d:Document {hash: $hash})-[r]->(neighbor:Document)
            WHERE NOT neighbor.hash IN $visited
            RETURN type(r) AS rel_type,
                   r.description AS description,
                   coalesce(r.strength, 5) AS strength,
                   neighbor.hash AS neighbor_hash,
                   neighbor.filepath AS filepath
            """,
            hash=doc_hash,
            visited=visited_list,
        )

        incoming = self.neo4j.query_graph(
            """
            MATCH (neighbor:Document)-[r]->(d:Document {hash: $hash})
            WHERE NOT neighbor.hash IN $visited
            RETURN type(r) AS rel_type,
                   r.description AS description,
                   coalesce(r.strength, 5) AS strength,
                   neighbor.hash AS neighbor_hash,
                   neighbor.filepath AS filepath
            """,
            hash=doc_hash,
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

        The intent's edge plan is a PRIORITY ORDER, not a filter: on-plan edge types
        are followed first (in plan order), but any other relationship edge is still
        eligible as a fallback (ranked after the plan), so the walk can reach a
        connected document even when its edge type isn't in the plan.

        Returns an ordered list of dicts: {hash, filepath, edge_description}.
        edge_description is None for seeds (they were retrieved, not followed).
        """
        edge_types = EDGE_PRIORITIES.get(intent, [])
        priority_rank = {etype: i for i, etype in enumerate(edge_types)}
        off_plan_rank = len(edge_types)  # off-plan edges sort after every on-plan type

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
                    "is_seed": True,
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
            for neighbor in self.get_neighbors(anchor["hash"], visited):
                rank = priority_rank.get(neighbor["rel_type"], off_plan_rank)
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
                    "rel_type": neighbor["rel_type"],
                    "is_seed": False,
                }
            )
            hops += 1

            for next_neighbor in self.get_neighbors(
                neighbor["neighbor_hash"], visited
            ):
                next_rank = priority_rank.get(next_neighbor["rel_type"], off_plan_rank)
                heapq.heappush(
                    heap, (next_rank, -next_neighbor["strength"], counter, next_neighbor)
                )
                counter += 1

        return path

    def augment_bridges(
        self, base: List[Dict[str, Any]], intent: str
    ) -> List[Dict[str, Any]]:
        """Augment-not-replace: keep the full RAG base, then ADD graph bridges.

        The base (RAG top-K) is always kept — so retrieval is never worse than flat
        RAG. We then collect documents one hop from ANY base doc along the intent's
        typed edges and add the strongest few as extra, framed context. A document
        reached from MULTIPLE base docs is a stronger bridge (the lite version of
        "connect-the-base"), so it is preferred. There is no deep best-first walk,
        so there is no goal-blind wandering — the graph only contributes connections
        that hang off the query-grounded base.
        """
        base_hashes = {d["hash"] for d in base}
        edge_types = EDGE_PRIORITIES.get(intent, [])
        priority_rank = {t: i for i, t in enumerate(edge_types)}
        off_plan = len(edge_types)

        # Gather 1-hop neighbors of the whole base, deduped by neighbor, counting
        # how many base docs each connects to and keeping its strongest edge.
        candidates: Dict[str, Dict[str, Any]] = {}
        for doc in base:
            for nb in self.get_neighbors(doc["hash"], base_hashes):
                cur = candidates.get(nb["neighbor_hash"])
                if cur is None:
                    cand = dict(nb)
                    cand["connects"] = 1
                    candidates[nb["neighbor_hash"]] = cand
                else:
                    cur["connects"] += 1
                    if nb["strength"] > cur["strength"]:
                        cur["strength"] = nb["strength"]
                        cur["rel_type"] = nb["rel_type"]
                        cur["description"] = nb["description"]

        # Rank: on-plan edge types first, then more base-connections, then strength.
        ranked = sorted(
            candidates.values(),
            key=lambda c: (priority_rank.get(c["rel_type"], off_plan),
                           -c["connects"], -c["strength"]),
        )

        path: List[Dict[str, Any]] = [
            {"hash": d["hash"], "filepath": d["filepath"],
             "edge_description": None, "is_seed": True}
            for d in base
        ]
        for c in ranked[:MAX_BRIDGES]:
            path.append({
                "hash": c["neighbor_hash"],
                "filepath": c["filepath"],
                "edge_description": c["description"],
                "rel_type": c["rel_type"],
                "is_seed": False,
            })
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

Documents (in retrieval order — each is labeled with why it was retrieved):
{context}

{config.ANSWER_INSTRUCTIONS}

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
        """Distinct entity_type labels present in the graph (cached per session).

        The classify call needs these to map "teams" → the real label, so it runs
        on (almost) every query; the types don't change between queries, so we fetch
        them once and reuse — one cheap Cypher round-trip, not one per question.
        """
        if getattr(self, "_entity_types_cache", None) is not None:
            return self._entity_types_cache
        try:
            rows = self.neo4j.query_graph(
                "MATCH (e:Entity) WHERE e.entity_type IS NOT NULL "
                "RETURN DISTINCT e.entity_type AS t ORDER BY t"
            )
            generic = [r["t"] for r in rows if r.get("t")]
            # The curated semantic-layer pivots are always offered as dimensions so
            # the classifier routes "which team / which component" to exact counts.
            semantic = [k for k in self.SEMANTIC_DIMENSIONS
                        if not any(k == g.lower() for g in generic)]
            self._entity_types_cache = semantic + generic
        except Exception:
            self._entity_types_cache = list(self.SEMANTIC_DIMENSIONS)
        return self._entity_types_cache

    def plan_thematic(self, query: str) -> Dict[str, Any]:
        """Fallback planner (one LLM call) for when the keyword backstop promoted a
        question to thematic but the combined classify() call produced no plan.

        Normally the plan rides along with classify() for free; this only fires on
        the rare promote-after-misclassification path. Returns {mode, entity_type,
        theme} — see _normalize_plan / run_thematic for what each does.
        """
        types = self._available_entity_types()
        types_str = ", ".join(types) if types else "(none available)"
        prompt = f"""A user asked a corpus-wide question about a knowledge graph of incident reports (RCAs) and change requests (CRs).

Question: "{query}"

Entity types stored as nodes in the graph: {types_str}

Choose ONE mode:
- "entity_count": a COUNT or RANKING of a kind of thing stored as entity nodes (teams,
  components, services...). Set entity_type to the best match above, or null for all.
- "theme_synthesis": needs reading across documents (patterns, contributing factors,
  which incidents match a theme). Set theme to a short lowercase keyword, or null.

Return ONLY valid JSON:
{{"mode": "entity_count" | "theme_synthesis", "entity_type": "<type or null>", "theme": "<keyword or null>"}}"""

        try:
            raw = self.llm.generate_json(prompt)
        except Exception:
            raw = {}
        return self._normalize_plan(raw)

    # Semantic-layer pivots: a dimension name -> (node label, edge type). These are
    # exact-count dimensions backed by first-class typed nodes (the read-wide
    # business pivots), as opposed to the generic Entity/MENTIONS aggregation.
    SEMANTIC_DIMENSIONS = {
        "component": ("Component", "AFFECTS"),
        "team": ("Team", "INVOLVED"),
    }

    def _aggregate_dimension(
        self, dimension: Any, top_n: int = THEMATIC_TOP_N
    ) -> List[Dict[str, Any]]:
        """Route a corpus-wide count to the right backing structure: the curated
        Component/Team semantic layer when the dimension is one of those, else the
        generic Entity/MENTIONS aggregation."""
        key = str(dimension).lower().strip() if dimension else None
        if key in self.SEMANTIC_DIMENSIONS:
            label, edge = self.SEMANTIC_DIMENSIONS[key]
            return self.aggregate_semantic(label, edge, top_n)
        return self.aggregate_entities(dimension, top_n)

    def aggregate_semantic(
        self, label: str, edge: str, top_n: int = THEMATIC_TOP_N
    ) -> List[Dict[str, Any]]:
        """Exact corpus-wide count of documents per Component/Team node.

        label/edge are whitelisted against SEMANTIC_DIMENSIONS before being put
        into the query string (Neo4j can't parameterize labels/edge types)."""
        valid = {(lbl, e) for lbl, e in self.SEMANTIC_DIMENSIONS.values()}
        if (label, edge) not in valid:
            return []
        return self.neo4j.query_graph(
            f"""
            MATCH (d:Document)-[:{edge}]->(x:{label})
            RETURN x.name AS name, '{label}' AS type,
                   count(DISTINCT d) AS doc_count,
                   collect(DISTINCT d.filepath)[..8] AS docs
            ORDER BY doc_count DESC, name ASC
            LIMIT $top_n
            """,
            top_n=top_n,
        )

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
    ) -> List[Dict[str, Any]]:
        """Collect the FULL set of documents for a theme — no best-first cap.

        Returns each document's distilled fingerprint (filepath, date, topics,
        entities) straight from the graph — NOT its full text. That is the whole
        speed trick: the expensive reading already happened at build time, so we
        aggregate over compact summaries (~tens of tokens each) instead of dumping
        dozens of full documents into the model.

        With a theme: match it against topics, entities, mentioned entity names, and
        filepath. Without one: fall back to all incident reports (RCA/*), since
        corpus-wide pattern questions are about incidents.
        """
        if theme:
            return self.neo4j.query_graph(
                """
                MATCH (d:Document)
                OPTIONAL MATCH (d)-[:MENTIONS]->(e:Entity)
                WITH d, collect(toLower(e.name)) AS enames
                WHERE any(t IN d.topics   WHERE toLower(t) CONTAINS $theme)
                   OR any(x IN d.entities WHERE toLower(x) CONTAINS $theme)
                   OR any(n IN enames     WHERE n CONTAINS $theme)
                   OR any(c IN d.components WHERE toLower(c) CONTAINS $theme)
                   OR toLower(d.filepath) CONTAINS $theme
                RETURN d.filepath AS filepath, d.date AS date, d.doc_type AS doc_type,
                       d.topics AS topics, d.entities AS entities,
                       d.components AS components, d.teams AS teams,
                       d.root_cause AS root_cause, d.resolution AS resolution,
                       d.status AS status, d.customer_impact AS customer_impact
                ORDER BY d.date DESC, d.filepath
                LIMIT $limit
                """,
                theme=theme.lower(),
                limit=limit,
            )
        return self.neo4j.query_graph(
            """
            MATCH (d:Document)
            WHERE toLower(d.filepath) STARTS WITH 'rca/'
            RETURN d.filepath AS filepath, d.date AS date, d.doc_type AS doc_type,
                   d.topics AS topics, d.entities AS entities,
                   d.components AS components, d.teams AS teams,
                   d.root_cause AS root_cause, d.resolution AS resolution,
                   d.status AS status, d.customer_impact AS customer_impact
            ORDER BY d.date DESC, d.filepath
            LIMIT $limit
            """,
            limit=limit,
        )

    def gather_all_rows(self) -> List[Dict[str, Any]]:
        """Every Document's fingerprint (no theme filter) — the breadth set for
        counting/patterns. Theme-keyword matching is intentionally dropped: it
        polluted the set with unrelated CRs whose topics happened to contain the
        theme word, which then crowded out the real incident docs."""
        return self.neo4j.query_graph(
            """
            MATCH (d:Document)
            RETURN d.filepath AS filepath, d.date AS date, d.doc_type AS doc_type,
                   d.topics AS topics, d.entities AS entities,
                   d.components AS components, d.teams AS teams,
                   d.root_cause AS root_cause, d.resolution AS resolution,
                   d.status AS status, d.customer_impact AS customer_impact
            ORDER BY d.date DESC, d.filepath
            """
        )

    def crs_connected_to_incidents(self) -> Set[str]:
        """Filepaths of CRs the graph links to an RCA by any Document-Document edge —
        the changes that caused, remediated, preceded, or otherwise relate to an
        incident. These are the 'directly relevant' CRs read in full text alongside
        the RCAs (MENTIONS edges end at Entity nodes, so they don't match here)."""
        rows = self.neo4j.query_graph(
            """
            MATCH (cr:Document)-[]-(rca:Document)
            WHERE toLower(cr.filepath)  STARTS WITH 'cr/'
              AND toLower(rca.filepath) STARTS WITH 'rca/'
            RETURN DISTINCT cr.filepath AS filepath
            """
        )
        return {r["filepath"] for r in rows}

    def _build_thematic_context(self, rows: List[Dict[str, Any]]) -> str:
        """Build a compact digest (one block per doc) from each node's stored
        incident facts. With the domain schema the block now carries root_cause,
        resolution, status, customer_impact, components and teams — so corpus-wide
        synthesis can answer "contributing factors / who resolved it / which are
        still open" from the fingerprint itself. Fields absent on older nodes
        (pre-rebuild) are simply skipped, so this degrades to topics+entities."""
        sections = []
        for i, r in enumerate(rows, 1):
            date = r.get("date") or "n/a"
            dtype = r.get("doc_type") or "doc"
            lines = [f"[Doc {i}: {r['filepath']} | {dtype} | date: {date}]"]

            def add(label: str, val: Any):
                if not val:
                    return
                if isinstance(val, list):
                    val = ", ".join(str(x) for x in val)
                lines.append(f"  {label}: {val}")

            add("topics", r.get("topics"))
            add("components", r.get("components"))
            add("teams", r.get("teams"))
            add("root cause", r.get("root_cause"))
            add("resolution", r.get("resolution"))
            add("customer impact", r.get("customer_impact"))
            add("status", r.get("status"))
            add("entities", r.get("entities"))  # longest, kept last
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def _rank_rows_by_relevance(
        self, query: str, rows: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Order the matching set by relevance to the query (most relevant first),
        so the hybrid reader can pick which few to read in full. Uses embeddings of
        each doc's fingerprint vs the query; degrades to the original order if
        embeddings are unavailable."""
        if len(rows) <= 1:
            return rows
        texts = []
        for r in rows:
            parts = list(r.get("topics") or []) + list(r.get("components") or [])
            parts += list(r.get("teams") or [])
            for f in ("root_cause", "resolution", "customer_impact"):
                if r.get(f):
                    parts.append(str(r[f]))
            texts.append(" ".join(parts) or r["filepath"])
        try:
            import numpy as np
            vecs = np.asarray(self.llm.embed([query] + texts), dtype=float)
            vecs /= np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9, None)
            sims = vecs[1:] @ vecs[0]
            return [rows[i] for i in np.argsort(-sims)]
        except Exception as e:
            print(f"  (relevance ranking unavailable: {e}; using gather order)")
            return rows

    def _read_fulltext(self, filepaths: List[str]) -> str:
        """Read the full text of each document, with a header per doc."""
        sections = []
        for i, fp in enumerate(filepaths, 1):
            try:
                text = (config.DOCUMENTS_DIR / fp).read_text(encoding="utf-8")
            except Exception:
                continue
            sections.append(f"[Document {i}: {fp}]\n\n{text}")
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

Write a brief, direct answer (1-3 sentences or a short ranked list) grounded in these exact numbers, naming the top entities and their counts. Lead with the answer; no preamble. You may use light Markdown (**bold**, "- " bullets). Do not invent or recompute any numbers beyond those given. Output only the answer, not your reasoning."""
        # Headroom for gpt-oss reasoning tokens (see THEMATIC_ANSWER_TOKENS note);
        # the prompt keeps the visible answer to a short ranked list.
        return self.llm.generate_text(prompt, max_tokens=THEMATIC_ANSWER_TOKENS)

    def answer_theme_synthesis(
        self, query: str, theme: Any, fulltext: str, summaries: str,
        n_full: int, n_total: int
    ) -> str:
        """Aggregate/pattern over the matching set in one LLM call, reading the most
        relevant docs in FULL and the rest as compact fingerprints (the hybrid)."""
        if not fulltext.strip() and not summaries.strip():
            suffix = f" for the theme '{theme}'." if theme else "."
            return (
                "No documents in the knowledge graph matched this question" + suffix
            )
        scope = f"documents related to '{theme}'" if theme else "all incident reports"
        full_block = (
            f"FULL TEXT of the {n_full} most relevant documents (use these for specific "
            f"facts — contributing factors, resolution detail, impact, timelines):\n\n{fulltext}"
            if fulltext.strip() else ""
        )
        summary_block = (
            f"\n\nCOMPACT SUMMARIES of the remaining matching documents (use these for "
            f"counting and breadth across the whole set):\n\n{summaries}"
            if summaries.strip() else ""
        )
        prompt = f"""You are answering a corpus-wide question by aggregating across {scope}. The matching set is {n_total} documents total. Below, the most relevant ones are given in FULL TEXT, and the rest as compact summaries.

Question: {query}

{full_block}{summary_block}

Instructions:
- Lead with the direct answer; no preamble or restating the question. Keep it concise — a short summary or tight ranked list, not an essay.
- For SPECIFIC facts (root causes, contributing factors, resolution steps, who was involved), rely on the full-text documents. For COUNTS and patterns across the set, use the summaries too (e.g. "X of {n_total} documents…"). Cite document names where relevant.
- You may use light Markdown (**bold**, "- " bullets); it will be rendered.
- Base claims only on what is provided. If a detail isn't present, say so rather than guessing.
- Output only the final answer, not your reasoning.

Answer:"""
        return self.llm.generate_text(prompt, max_tokens=THEMATIC_ANSWER_TOKENS)

    def run_thematic(
        self, query: str, plan: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Answer a corpus-wide question via aggregation, not traversal.

        `plan` normally arrives free from classify(); if absent (the keyword-promotion
        path), we fall back to a dedicated plan_thematic() call.
        """
        timing: Dict[str, float] = {}
        if plan is None:
            with _timed(timing, "plan"):
                plan = self.plan_thematic(query)

        if plan["mode"] == "entity_count":
            with _timed(timing, "aggregate"):
                counts = self._aggregate_dimension(plan["entity_type"])
            if counts:
                with _timed(timing, "answer"):
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
                    "timing": {k: round(v, 3) for k, v in timing.items()},
                }
            # No entities of that type — fall back to reading by theme.
            plan = {"mode": "theme_synthesis", "entity_type": None,
                    "theme": plan.get("entity_type")}

        # RCA-anchored hybrid read. The incident reports (RCAs) are the ground truth
        # for incident questions, so ALL of them are read in FULL TEXT, plus the CRs
        # the graph connects to an incident (the relevant changes). Everything else
        # is a compact fingerprint for breadth/counting. This replaces theme-keyword
        # gathering, which polluted the full-text slots with unrelated CRs and pushed
        # the real RCAs down into lossy fingerprints (the cause of low completeness).
        with _timed(timing, "gather"):
            all_rows = self.gather_all_rows()
            connected = self.crs_connected_to_incidents()
        filepaths = [r["filepath"] for r in all_rows]

        def _is_rca(r):
            return str(r["filepath"]).lower().startswith("rca/")

        rca_rows = [r for r in all_rows if _is_rca(r)]
        cr_rows = [r for r in all_rows
                   if not _is_rca(r) and r["filepath"] in connected]
        # RCAs are always read in full; bound the connected CRs read in full by
        # relevance so the full-text block stays a reasonable size.
        if len(cr_rows) > THEMATIC_FULLTEXT_N:
            with _timed(timing, "rank"):
                cr_rows = self._rank_rows_by_relevance(query, cr_rows)[:THEMATIC_FULLTEXT_N]
        deep_rows = rca_rows + cr_rows
        deep_paths = {r["filepath"] for r in deep_rows}
        rest_rows = [r for r in all_rows if r["filepath"] not in deep_paths]

        fulltext = self._read_fulltext([r["filepath"] for r in deep_rows])
        summaries = self._build_thematic_context(rest_rows)
        with _timed(timing, "answer"):
            answer = self.answer_theme_synthesis(
                query, plan.get("theme"), fulltext, summaries,
                len(deep_rows), len(filepaths),
            )
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
            "timing": {k: round(v, 3) for k, v in timing.items()},
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
        timing: Dict[str, float] = {}
        t_start = time.perf_counter()

        with _timed(timing, "classify"):
            routed = self.classify(query)
        intent = routed["intent"]

        if intent == "thematic":
            # The plan rides along with classify() for free — no second LLM call.
            result = self.run_thematic(query, routed.get("thematic_plan"))
            result.setdefault("timing", {})["classify"] = round(timing["classify"], 3)
            result["timing"]["total"] = round(time.perf_counter() - t_start, 3)
            return result

        with _timed(timing, "seeds"):
            base = self.find_seeds(query, size=AUGMENT_BASE_K)
        with _timed(timing, "traverse"):
            path = self.augment_bridges(base, intent)
        with _timed(timing, "context"):
            context = self.build_context(path)
        with _timed(timing, "answer"):
            answer = self.generate_answer(query, intent, context)
        timing["total"] = time.perf_counter() - t_start

        return {
            "query": query,
            "intent": intent,
            "intent_description": INTENT_DESCRIPTIONS.get(intent, ""),
            "edge_priority": EDGE_PRIORITIES.get(intent, []),
            "seeds": [d["filepath"] for d in base],
            "path": [
                {
                    "filepath": node["filepath"],
                    "edge_description": node.get("edge_description"),
                    "rel_type": node.get("rel_type"),
                    "is_seed": node.get("is_seed", node.get("edge_description") is None),
                }
                for node in path
            ],
            "answer": answer,
            "timing": {k: round(v, 3) for k, v in timing.items()},
        }

    def ask(self, query: str) -> str:
        """Run a query and print a human-readable trace (CLI entry point)."""
        print(f"\nQuery: {query}")
        result = self.run_query(query)

        timing_line = _format_timing(result.get("timing", {}))
        if timing_line:
            print(f"Timing: {timing_line}")

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
