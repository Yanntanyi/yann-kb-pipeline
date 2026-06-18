"""index_es.py — Build the Elasticsearch index used for seed retrieval.

Reads every Document node from Neo4j (so ES ids match the graph's hashes),
loads each document's full text, optionally computes a dense embedding, and
bulk-indexes everything into Elasticsearch.

Run this after the graph pipeline (main.py) has populated Neo4j, and re-run it
whenever the corpus changes.

Usage:
  python index_es.py              # create index if missing, then index
  python index_es.py --recreate   # drop and rebuild the index from scratch
"""

import sys

import config
from es_handler import ElasticsearchHandler
from llm_client import get_llm_client
from neo4j_handler import Neo4jHandler


def load_documents(neo4j: Neo4jHandler):
    """Pull every Document node and read its full text from disk.

    Documents whose file can't be read are skipped with a warning rather than
    aborting the whole index build.
    """
    rows = neo4j.query_graph(
        "MATCH (d:Document) "
        "RETURN d.hash AS hash, d.filepath AS filepath, "
        "d.topics AS topics, d.entities AS entities"
    )

    docs = []
    for row in rows:
        try:
            text = (config.DOCUMENTS_DIR / row["filepath"]).read_text(encoding="utf-8")
        except Exception as e:
            print(f"  Warning: could not read {row['filepath']}: {e}")
            continue

        docs.append(
            {
                "hash": row["hash"],
                "filepath": row["filepath"],
                "text": text,
                "topics": " ".join(row.get("topics") or []),
                "entities": " ".join(row.get("entities") or []),
            }
        )
    return docs


def main():
    recreate = "--recreate" in sys.argv

    neo4j = Neo4jHandler()
    es = ElasticsearchHandler()

    try:
        print("Loading documents from Neo4j...")
        docs = load_documents(neo4j)
        if not docs:
            raise RuntimeError(
                "No Document nodes in Neo4j. Run the pipeline (main.py) first."
            )
        print(f"  {len(docs)} documents loaded.")

        if config.ES_USE_DENSE:
            print(f"Embedding documents with {config.WATSONX_EMBED_MODEL}...")
            llm = get_llm_client()
            # Embed the full text of each document, in corpus order.
            vectors = llm.embed([d["text"] for d in docs])
            if len(vectors) != len(docs):
                raise RuntimeError(
                    f"Embedding count ({len(vectors)}) != document count ({len(docs)})."
                )
            for doc, vec in zip(docs, vectors):
                doc["embedding"] = vec
            print(f"  Embedded {len(vectors)} documents (dim={len(vectors[0])}).")

        print(f"Creating index '{config.ES_INDEX}' (recreate={recreate})...")
        es.create_index(recreate=recreate)

        print("Bulk-indexing...")
        es.bulk_index(docs)
        print(f"Done. Indexed {len(docs)} documents into '{config.ES_INDEX}'.")
    finally:
        neo4j.close()


if __name__ == "__main__":
    main()
