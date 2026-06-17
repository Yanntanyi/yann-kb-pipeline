"""Neo4j database handler.

Wraps the Neo4j Python driver and exposes one method per graph operation.
All writes use MERGE so re-running any phase is safe — no duplicate nodes
or edges will be created.

New in yann-pipeline (vs suhas-pipeline):
  - create_entity_node        → Entity nodes as first-class Neo4j nodes
  - create_mentions_relationship → Document-[:MENTIONS]->Entity edges
  - create_temporal_edge      → Document-[:PRECEDED_BY]->Document edges
  - create_document_node now accepts a 'date' property
  - create_relationship whitelists rel_type to prevent Cypher injection
    (dynamic relationship types require string formatting — whitelisting
     keeps it safe without needing the APOC plugin)
"""

from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase

import config

# The only relationship types the LLM is allowed to produce.
# Any other string is coerced to RELATED_TO before hitting Neo4j.
VALID_REL_TYPES = {
    "EXTENDS",
    "CONTRADICTS",
    "SUPPORTS",
    "REFERENCES",
    "PROVIDES_CONTEXT_FOR",
    "SHARES_DOMAIN_WITH",
    "IMPLEMENTS",
}


class Neo4jHandler:
    """Handle all Neo4j read/write operations for the knowledge graph."""

    def __init__(self):
        self.driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        )

    # ── Schema ────────────────────────────────────────────────────────────────

    def initialize_schema(self):
        """Create uniqueness constraints so MERGE operations are fast and safe."""
        with self.driver.session() as session:
            session.run(
                "CREATE CONSTRAINT document_hash IF NOT EXISTS "
                "FOR (d:Document) REQUIRE d.hash IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT entity_name IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.name IS UNIQUE"
            )

    # ── Document nodes ────────────────────────────────────────────────────────

    def create_document_node(
        self,
        doc_hash: str,
        filepath: str,
        entities: List[str],
        topics: List[str],
        stance: str,
        date: Optional[str] = None,
    ):
        """Create or update a Document node.

        'date' is stored as a string (YYYY-MM-DD) so Neo4j doesn't need
        temporal types — Phase 5 does all date arithmetic in Python.
        """
        with self.driver.session() as session:
            session.run(
                """
                MERGE (d:Document {hash: $hash})
                SET d.filepath   = $filepath,
                    d.entities   = $entities,
                    d.topics     = $topics,
                    d.stance     = $stance,
                    d.date       = $date
                """,
                hash=doc_hash,
                filepath=filepath,
                entities=entities,
                topics=topics,
                stance=stance,
                date=date,
            )

    # ── Document→Document relationship edges ─────────────────────────────────

    def create_relationship(
        self,
        hash1: str,
        hash2: str,
        rel_type: str,
        strength: int,
        description: str,
        confidence: str,
        directionality: str = "symmetric",
    ):
        """Create a typed, directed relationship edge between two Document nodes.

        rel_type is whitelisted before being interpolated into the Cypher string
        to prevent injection — Neo4j doesn't support dynamic relationship types
        as query parameters without APOC.
        """
        if rel_type not in VALID_REL_TYPES:
            rel_type = "RELATED_TO"

        cypher = f"""
        MATCH (d1:Document {{hash: $hash1}})
        MATCH (d2:Document {{hash: $hash2}})
        MERGE (d1)-[r:{rel_type}]->(d2)
        SET r.strength      = $strength,
            r.description   = $description,
            r.confidence    = $confidence,
            r.directionality = $directionality
        """
        with self.driver.session() as session:
            session.run(
                cypher,
                hash1=hash1,
                hash2=hash2,
                strength=strength,
                description=description,
                confidence=confidence,
                directionality=directionality,
            )

    # ── Entity nodes ──────────────────────────────────────────────────────────

    def create_entity_node(self, canonical_name: str, mention_count: int = 0):
        """Create or update an Entity node.

        Parses the Phase 1 format 'EntityName (Type)' to split name and type
        into separate properties so you can query by type in Neo4j:
          MATCH (e:Entity {entity_type: 'Organization'}) RETURN e.name
        """
        if "(" in canonical_name and canonical_name.endswith(")"):
            parts = canonical_name.rsplit("(", 1)
            name = parts[0].strip()
            entity_type = parts[1][:-1].strip()
        else:
            name = canonical_name
            entity_type = "Unknown"

        with self.driver.session() as session:
            session.run(
                """
                MERGE (e:Entity {name: $name})
                SET e.entity_type    = $entity_type,
                    e.mention_count  = $mention_count,
                    e.canonical_name = $canonical_name
                """,
                name=name,
                entity_type=entity_type,
                mention_count=mention_count,
                canonical_name=canonical_name,
            )

    def create_mentions_relationship(self, doc_hash: str, canonical_name: str):
        """Create a MENTIONS edge from a Document node to an Entity node."""
        if "(" in canonical_name and canonical_name.endswith(")"):
            name = canonical_name.rsplit("(", 1)[0].strip()
        else:
            name = canonical_name

        with self.driver.session() as session:
            session.run(
                """
                MATCH (d:Document {hash: $doc_hash})
                MATCH (e:Entity {name: $name})
                MERGE (d)-[:MENTIONS]->(e)
                """,
                doc_hash=doc_hash,
                name=name,
            )

    # ── Temporal edges ────────────────────────────────────────────────────────

    def create_temporal_edge(
        self, hash_earlier: str, hash_later: str, days_apart: Optional[int] = None
    ):
        """Create a PRECEDED_BY edge from the earlier document to the later one.

        days_apart is stored on the edge so queries can filter by recency:
          MATCH (d1)-[r:PRECEDED_BY]->(d2) WHERE r.days_apart < 7
        """
        with self.driver.session() as session:
            session.run(
                """
                MATCH (d1:Document {hash: $hash_earlier})
                MATCH (d2:Document {hash: $hash_later})
                MERGE (d1)-[r:PRECEDED_BY]->(d2)
                SET r.days_apart = $days_apart
                """,
                hash_earlier=hash_earlier,
                hash_later=hash_later,
                days_apart=days_apart,
            )

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_all_documents(self) -> List[Any]:
        """Return all Document nodes."""
        with self.driver.session() as session:
            result = session.run("MATCH (d:Document) RETURN d")
            return [record["d"] for record in result]

    def query_graph(self, cypher: str, **params) -> List[Dict]:
        """Execute a read-only Cypher query and return results as dicts."""
        with self.driver.session() as session:
            result = session.run(cypher, **params)
            return [record.data() for record in result]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self):
        """Close the Neo4j driver connection."""
        self.driver.close()
