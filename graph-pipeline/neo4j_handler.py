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

# The only Document→Document relationship types the LLM (Phase 4) is allowed to
# produce. Any other string is coerced to RELATED_TO before hitting Neo4j.
# Incident-domain ontology: the types that map to how SREs actually reason about
# incidents and changes. The old generic set (EXTENDS/SUPPORTS/SHARES_DOMAIN_WITH/
# IMPLEMENTS/CONTRADICTS) was dropped — SHARES_DOMAIN_WITH in particular linked
# everything in a tech area, manufacturing the over-connected hubs that made the
# traversal goal-blind. (MENTIONS and PRECEDED_BY are created structurally in
# Phase 5, not by the LLM, so they are not in this whitelist.)
VALID_REL_TYPES = {
    "CAUSED_BY",            # a change/event caused the problem in the other doc
    "REMEDIATED_BY",        # the other doc is the change/action that fixed this one
    "RECURRENCE_OF",        # same underlying failure mode recurring
    "REFERENCES",           # one doc explicitly cites/links the other
    "PROVIDES_CONTEXT_FOR", # one gives background needed to understand the other
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
            # Semantic layer: Component / Team as first-class nodes (the read-wide
            # pivots — "which component fails most", "which team is engaged most").
            session.run(
                "CREATE CONSTRAINT component_name IF NOT EXISTS "
                "FOR (c:Component) REQUIRE c.name IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT team_name IF NOT EXISTS "
                "FOR (t:Team) REQUIRE t.name IS UNIQUE"
            )

    # ── Document nodes ────────────────────────────────────────────────────────

    def create_document_node(
        self,
        doc_hash: str,
        filepath: str,
        properties: Dict[str, Any],
    ):
        """Create or update a Document node from a Phase 1 extraction map.

        Every key in `properties` (entities, topics, date, doc_type, root_cause,
        resolution, status, customer_impact, components, teams, severity, …) is
        written as a node property via `SET d += $props`. This is future-proof:
        add a field to the Phase 1 schema and it lands on the node with no change
        here. List values store as string arrays; None values are skipped (Neo4j
        treats a null in a `+=` map as "remove that property").

        Dates are stored as strings (YYYY-MM-DD) so Neo4j needs no temporal types —
        Phase 5 does all date arithmetic in Python.
        """
        props = {k: v for k, v in (properties or {}).items()
                 if k not in ("hash", "filepath") and v is not None}
        with self.driver.session() as session:
            session.run(
                """
                MERGE (d:Document {hash: $hash})
                SET d.filepath = $filepath
                SET d += $props
                """,
                hash=doc_hash,
                filepath=filepath,
                props=props,
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

    # ── Semantic layer: Component / Team nodes (the read-wide pivots) ──────────

    def create_component_node(self, name: str):
        """Create or update a Component node (an affected/changed system)."""
        with self.driver.session() as session:
            session.run("MERGE (c:Component {name: $name})", name=name)

    def create_team_node(self, name: str):
        """Create or update a Team node (a team/org/person involved)."""
        with self.driver.session() as session:
            session.run("MERGE (t:Team {name: $name})", name=name)

    def link_affects(self, doc_hash: str, name: str):
        """Document-[:AFFECTS]->Component — this doc affected/changed this component."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (d:Document {hash: $doc_hash})
                MATCH (c:Component {name: $name})
                MERGE (d)-[:AFFECTS]->(c)
                """,
                doc_hash=doc_hash,
                name=name,
            )

    def link_involved(self, doc_hash: str, name: str):
        """Document-[:INVOLVED]->Team — this team/person was involved in this doc."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (d:Document {hash: $doc_hash})
                MATCH (t:Team {name: $name})
                MERGE (d)-[:INVOLVED]->(t)
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

    def query_graph(self, cypher: str, **params) -> List[Dict[str, Any]]:
        """Execute a read-only Cypher query and return results as dicts."""
        with self.driver.session() as session:
            result = session.run(cypher, **params)
            return [record.data() for record in result]

    def clear_graph(self):
        """Delete every node and relationship. Used by a from-scratch rebuild so a
        schema/ontology change can't leave stale nodes, edges, or properties behind
        (Phase 5 writes with MERGE, which never removes old data)."""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self):
        """Close the Neo4j driver connection."""
        self.driver.close()
