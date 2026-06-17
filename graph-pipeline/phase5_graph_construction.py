"""Phase 5: Neo4j knowledge graph construction.

Takes all previous phase outputs and writes the final graph to Neo4j.

Key design choices carried over from suhas-pipeline:
  - Threshold filtering: NONE relationships, low strength, and low confidence
    are all dropped before anything touches Neo4j
  - Degree cap: prevents any one document becoming a hub by keeping only the
    top MAX_DEGREE_PER_NODE strongest connections per document node
  - Idempotent MERGE operations: safe to re-run without creating duplicates

New in yann-pipeline:
  - Document nodes now store the 'date' property (extracted in Phase 1)
  - Entity nodes: every canonical entity from Phase 2 becomes a real Neo4j node
    with a MENTIONS edge from each document that references it. This is the fix
    for the flat graph problem — services/teams/clusters are now traversable
  - PRECEDED_BY temporal edges: for each related document pair where both docs
    have a date, a directed PRECEDED_BY edge is created from the earlier to the
    later. Enables queries like 'what changes happened before this incident?'
"""

from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

from neo4j_handler import Neo4jHandler
import config


def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse a YYYY-MM-DD string into a datetime object, or None if invalid."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


class GraphConstructor:
    """Construct the knowledge graph in Neo4j with quality filtering."""

    def __init__(self):
        self.neo4j = Neo4jHandler()

    # ── Threshold filtering ───────────────────────────────────────────────────

    def filter_by_thresholds(self, scored_relationships: List[Dict]) -> List[Dict]:
        """Drop relationships that don't meet quality bar before writing to Neo4j."""
        filtered = []
        for rel in scored_relationships:
            relationship = rel["relationship"]

            if relationship["relationship_type"] == "NONE":
                continue
            if relationship["strength"] < config.MIN_RELATIONSHIP_STRENGTH:
                continue
            if relationship["confidence"] == "low":
                continue

            filtered.append(rel)
        return filtered

    # ── Degree cap ────────────────────────────────────────────────────────────

    def apply_degree_cap(self, relationships: List[Dict]) -> List[Dict]:
        """Keep only the top MAX_DEGREE_PER_NODE strongest edges per document."""
        degree_count: Dict[str, List] = defaultdict(list)

        for rel in relationships:
            strength = rel["relationship"]["strength"]
            degree_count[rel["hash1"]].append((strength, rel))
            degree_count[rel["hash2"]].append((strength, rel))

        kept = set()
        for doc_hash, connections in degree_count.items():
            connections.sort(key=lambda x: x[0], reverse=True)
            for strength, rel in connections[: config.MAX_DEGREE_PER_NODE]:
                kept.add(tuple(sorted([rel["hash1"], rel["hash2"]])))

        return [
            rel for rel in relationships
            if tuple(sorted([rel["hash1"], rel["hash2"]])) in kept
        ]

    # ── Graph construction ────────────────────────────────────────────────────

    def create_graph(
        self,
        extractions: Dict[str, Dict],
        scored_relationships: List[Dict],
        canonical_index: Optional[Dict] = None,
    ):
        """Write the complete knowledge graph to Neo4j in five steps.

        Steps:
          1  Filter + cap relationships
          2  Create Document nodes
          3  Create Document→Document relationship edges
          4  Create Entity nodes + MENTIONS edges       ← new
          5  Create PRECEDED_BY temporal edges          ← new
        """
        self.neo4j.initialize_schema()

        # ── Step 1: Filter ────────────────────────────────────────────────────
        print("\nFiltering relationships by quality thresholds...")
        filtered = self.filter_by_thresholds(scored_relationships)
        print(f"  After threshold filter: {len(filtered)} relationships")

        print("Applying degree cap to prevent hub documents...")
        final_relationships = self.apply_degree_cap(filtered)
        print(f"  After degree cap:       {len(final_relationships)} relationships")

        # ── Step 2: Document nodes ────────────────────────────────────────────
        print(f"\nCreating {len(extractions)} document nodes...")
        for doc_hash, doc_data in extractions.items():
            extraction = doc_data["extraction"]
            self.neo4j.create_document_node(
                doc_hash=doc_hash,
                filepath=doc_data["filepath"],
                entities=extraction["entities"],
                topics=extraction["topics"],
                stance=extraction["stance"],
                date=extraction.get("date"),   # new — stored from Phase 1
            )
        print("Document nodes created")

        # ── Step 3: Document→Document edges ──────────────────────────────────
        print(f"\nCreating {len(final_relationships)} relationship edges...")
        for rel in final_relationships:
            relationship = rel["relationship"]
            directionality = relationship.get("directionality", "symmetric")

            # Phase 4 uses directionality to signal edge direction
            if directionality == "doc2_to_doc1":
                h1, h2 = rel["hash2"], rel["hash1"]
            else:
                h1, h2 = rel["hash1"], rel["hash2"]

            self.neo4j.create_relationship(
                hash1=h1,
                hash2=h2,
                rel_type=relationship["relationship_type"],
                strength=relationship["strength"],
                description=relationship["description"],
                confidence=relationship["confidence"],
                directionality=directionality,
            )
        print("Relationship edges created")

        # ── Step 4: Entity nodes + MENTIONS edges (new) ───────────────────────
        if canonical_index:
            print(f"\nCreating {len(canonical_index)} entity nodes + MENTIONS edges...")
            for canonical_name, entity_data in canonical_index.items():
                self.neo4j.create_entity_node(
                    canonical_name=canonical_name,
                    mention_count=entity_data["mention_count"],
                )
                for doc_hash in entity_data["document_hashes"]:
                    if doc_hash in extractions:
                        self.neo4j.create_mentions_relationship(
                            doc_hash=doc_hash,
                            canonical_name=canonical_name,
                        )
            print("Entity nodes and MENTIONS edges created")
        else:
            print("\nNo canonical_index provided — skipping entity nodes")

        # ── Step 5: PRECEDED_BY temporal edges (new) ──────────────────────────
        print("\nCreating PRECEDED_BY temporal edges...")
        temporal_count = self._create_temporal_edges(extractions, final_relationships)
        print(f"  Created {temporal_count} PRECEDED_BY edges")

        self.print_graph_statistics()

    def _create_temporal_edges(
        self, extractions: Dict[str, Dict], relationships: List[Dict]
    ) -> int:
        """Add PRECEDED_BY edges for related pairs where both docs have a date.

        Only creates temporal edges between documents that already share a
        relationship edge — we want 'this change preceded this incident',
        not 'every older document preceded every newer one'.
        """
        count = 0
        for rel in relationships:
            h1, h2 = rel["hash1"], rel["hash2"]

            if h1 not in extractions or h2 not in extractions:
                continue

            date1 = parse_date(extractions[h1]["extraction"].get("date"))
            date2 = parse_date(extractions[h2]["extraction"].get("date"))

            if date1 is None or date2 is None:
                continue  # can't establish ordering without both dates
            if date1 == date2:
                continue  # same day — no meaningful ordering

            days_apart = abs((date2 - date1).days)

            if date1 < date2:
                self.neo4j.create_temporal_edge(h1, h2, days_apart)
            else:
                self.neo4j.create_temporal_edge(h2, h1, days_apart)

            count += 1

        return count

    # ── Statistics ────────────────────────────────────────────────────────────

    def print_graph_statistics(self):
        """Print a summary of what was written to Neo4j."""
        all_docs = self.neo4j.get_all_documents()

        print("\n" + "=" * 60)
        print("KNOWLEDGE GRAPH STATISTICS")
        print("=" * 60)
        print(f"Total document nodes: {len(all_docs)}")

        rel_counts = self.neo4j.query_graph(
            "MATCH ()-[r]->() RETURN type(r) as rel_type, count(r) as count "
            "ORDER BY count DESC"
        )
        print("\nEdges by type:")
        for record in rel_counts:
            print(f"  {record['rel_type']}: {record['count']}")

        entity_count = self.neo4j.query_graph(
            "MATCH (e:Entity) RETURN count(e) as count"
        )
        if entity_count:
            print(f"\nEntity nodes: {entity_count[0]['count']}")

        avg_degree = self.neo4j.query_graph(
            "MATCH (d:Document) OPTIONAL MATCH (d)-[r]-() "
            "WITH d, count(r) as degree RETURN avg(degree) as avg_degree"
        )
        if avg_degree and avg_degree[0].get("avg_degree"):
            print(f"Avg connections/document: {avg_degree[0]['avg_degree']:.2f}")

        print("=" * 60)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self):
        """Close the Neo4j connection."""
        self.neo4j.close()


if __name__ == "__main__":
    from phase1_extraction import DocumentExtractor
    from phase2_normalization import EntityNormalizer
    from phase4_relationship_scoring import RelationshipScorer

    extractor = DocumentExtractor()
    extractions = extractor.load_extractions()

    normalizer = EntityNormalizer()
    entity_mapping = normalizer.load_normalization()

    scorer = RelationshipScorer()
    scored_relationships = scorer.load_scored_relationships()

    if not extractions or not entity_mapping or not scored_relationships:
        print("Missing results from previous phases. Run phases 1-4 first.")
        exit(1)

    normalized_extractions = normalizer.apply_normalization(extractions, entity_mapping)
    canonical_index = normalizer.build_canonical_index(normalized_extractions, entity_mapping)

    constructor = GraphConstructor()
    try:
        constructor.create_graph(normalized_extractions, scored_relationships, canonical_index)
        print("\nPhase 5 complete: Knowledge graph constructed in Neo4j")
    finally:
        constructor.close()
