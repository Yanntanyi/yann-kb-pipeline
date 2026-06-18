# Are both services up?
curl -s localhost:9200 >/dev/null && echo "ES up" || echo "ES DOWN"

# Does Neo4j already have the graph? (counts Document nodes)
python3 -c "from neo4j_handler import Neo4jHandler; n=Neo4jHandler(); print('Documents:', n.query_graph('MATCH (d:Document) RETURN count(d) AS c')[0]['c']); n.close()"
