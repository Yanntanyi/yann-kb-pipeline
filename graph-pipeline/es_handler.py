"""Elasticsearch handler for hybrid seed retrieval.

Provides BM25 (lexical) and dense kNN (semantic) search over the document
corpus, fused with Reciprocal Rank Fusion. RRF is done client-side in Python
rather than via Elasticsearch's `rrf` retriever so this works on any 8.x cluster
without depending on a specific license tier or version.

Documents are keyed by their Neo4j Document `hash`, so search results map
directly back to graph nodes for traversal.

Talks to Elasticsearch over its REST API with `requests` — no extra dependency
beyond what the pipeline already uses.
"""

import json
from typing import Any, Dict, List, Optional

import requests

import config


class ElasticsearchHandler:
    """Thin REST wrapper around an Elasticsearch index for hybrid retrieval."""

    def __init__(self):
        self.url = config.ES_URL.rstrip("/")
        self.index = config.ES_INDEX
        self.use_dense = config.ES_USE_DENSE
        self.rrf_k = config.ES_RRF_K

        self.auth = None
        if config.ES_BASIC_AUTH and ":" in config.ES_BASIC_AUTH:
            user, _, password = config.ES_BASIC_AUTH.partition(":")
            self.auth = (user, password)

    # ── Internal request helper ───────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = requests.request(
            method,
            f"{self.url}/{path.lstrip('/')}",
            auth=self.auth,
            timeout=kwargs.pop("timeout", 60),
            **kwargs,
        )
        response.raise_for_status()
        return response

    # ── Index lifecycle (used by index_es.py) ─────────────────────────────────

    def create_index(self, recreate: bool = False):
        """Create the document index, optionally dropping an existing one first.

        The mapping always defines BM25 text fields; the dense_vector field is
        only added when dense retrieval is enabled (its dims must match
        config.EMBEDDING_DIM).
        """
        if recreate and self.index_exists():
            self._request("DELETE", self.index)

        if self.index_exists():
            return

        mappings: Dict[str, Any] = {
            "properties": {
                "hash": {"type": "keyword"},
                "filepath": {"type": "keyword"},
                "text": {"type": "text"},
                "topics": {"type": "text"},
                "entities": {"type": "text"},
            }
        }
        if self.use_dense:
            mappings["properties"]["embedding"] = {
                "type": "dense_vector",
                "dims": config.EMBEDDING_DIM,
                "index": True,
                "similarity": "cosine",
            }

        self._request("PUT", self.index, json={"mappings": mappings})

    def index_exists(self) -> bool:
        response = requests.head(
            f"{self.url}/{self.index}", auth=self.auth, timeout=30
        )
        return response.status_code == 200

    def bulk_index(self, docs: List[Dict[str, Any]]):
        """Bulk-index documents. Each doc dict must contain 'hash' and 'filepath';
        'text', 'topics', 'entities', and (if dense) 'embedding' are optional.
        """
        if not docs:
            return

        lines = []
        for doc in docs:
            lines.append({"index": {"_index": self.index, "_id": doc["hash"]}})
            lines.append(doc)

        # NDJSON body, trailing newline required by the _bulk API.
        body = "\n".join(json.dumps(line) for line in lines) + "\n"
        self._request(
            "POST",
            "_bulk",
            data=body,
            headers={"Content-Type": "application/x-ndjson"},
        )
        # Make the new docs searchable immediately.
        self._request("POST", f"{self.index}/_refresh")

    # ── Search ─────────────────────────────────────────────────────────────────

    def _search_bm25(self, query_text: str, size: int) -> List[Dict[str, Any]]:
        body = {
            "size": size,
            "_source": ["filepath"],
            "query": {
                "multi_match": {
                    "query": query_text,
                    "fields": ["text", "topics^1.5", "entities^2"],
                    "type": "best_fields",
                }
            },
        }
        hits = self._request("POST", f"{self.index}/_search", json=body).json()
        return self._hits_to_rows(hits)

    def _search_knn(self, query_vector: List[float], size: int) -> List[Dict[str, Any]]:
        body = {
            "size": size,
            "_source": ["filepath"],
            "knn": {
                "field": "embedding",
                "query_vector": query_vector,
                "k": size,
                "num_candidates": max(size * 10, 100),
            },
        }
        hits = self._request("POST", f"{self.index}/_search", json=body).json()
        return self._hits_to_rows(hits)

    @staticmethod
    def _hits_to_rows(response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = []
        for hit in response_json.get("hits", {}).get("hits", []):
            rows.append(
                {
                    "hash": hit["_id"],
                    "filepath": (hit.get("_source") or {}).get("filepath"),
                    "score": hit.get("_score"),
                }
            )
        return rows

    def hybrid_search(
        self,
        query_text: str,
        query_vector: Optional[List[float]],
        size: int,
    ) -> List[Dict[str, Any]]:
        """Return the top `size` documents by BM25 + dense kNN fused with RRF.

        Falls back to BM25-only when dense is disabled or no vector is supplied.
        Each result is {hash, filepath}, ordered best first.
        """
        # Retrieve a deeper pool per list than we return, so fusion has room to
        # reward documents that rank well in both.
        pool = max(size * 5, 20)

        bm25 = self._search_bm25(query_text, pool)
        if not self.use_dense or query_vector is None:
            return [{"hash": r["hash"], "filepath": r["filepath"]} for r in bm25[:size]]

        knn = self._search_knn(query_vector, pool)
        fused = self._rrf_fuse([bm25, knn])
        return [{"hash": r["hash"], "filepath": r["filepath"]} for r in fused[:size]]

    def _rrf_fuse(self, ranked_lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Reciprocal Rank Fusion: score = Σ 1/(k + rank) across lists."""
        scores: Dict[str, float] = {}
        meta: Dict[str, Dict[str, Any]] = {}

        for ranked in ranked_lists:
            for rank, row in enumerate(ranked):
                key = row["hash"]
                scores[key] = scores.get(key, 0.0) + 1.0 / (self.rrf_k + rank)
                meta.setdefault(key, row)

        ordered = sorted(scores, key=lambda h: scores[h], reverse=True)
        return [meta[h] for h in ordered]
