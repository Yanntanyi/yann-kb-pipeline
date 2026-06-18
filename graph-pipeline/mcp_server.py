"""mcp_server.py — Model Context Protocol server for the incident knowledge graph.

Exposes the graph-query engine as MCP tools, so any MCP client — IBM's Bob,
Claude Code, or a watsonx Orchestrate agent — can ask the incident knowledge
base questions and read source documents through a standard tool call.

It reuses run_query() from ask.py, so the MCP path, the web UI, and the CLI all
share the exact same retrieval and answer logic. There is one source of truth.

Run it (stdio transport — how a local MCP client launches it as a subprocess):
  pip install mcp
  python3 mcp_server.py

Register it with an MCP client (Bob / Claude Code) by pointing the client at
this script. Two things matter: use the *venv's* Python as the command (so mcp
and the pipeline's dependencies are importable), and make sure Neo4j and
Elasticsearch are running and reachable from wherever the client launches it.
Example client config:
  {
    "mcpServers": {
      "ups-watson": {
        "command": "/ABSOLUTE/PATH/graph-pipeline/venv/bin/python3",
        "args": ["/ABSOLUTE/PATH/graph-pipeline/mcp_server.py"],
        "env": {
          "WATSONX_API_KEY": "...",
          "WATSONX_PROJECT_ID": "..."
        }
      }
    }
  }
"""

import sys
from contextlib import redirect_stdout

from mcp.server.fastmcp import FastMCP

import config
from ask import KnowledgeGraphQuerier

mcp = FastMCP("ups-watson")

# Build the querier once at startup — connects to Neo4j, Elasticsearch, and the
# LLM, exactly like the CLI and web UI do.
querier = KnowledgeGraphQuerier()


@mcp.tool()
def query_incidents(question: str) -> str:
    """Answer a question about UPS cloud incidents using the knowledge graph.

    Covers change requests (CRs) and root cause analyses (RCAs). Use this for
    questions about what caused an outage, how an issue was resolved, what
    changed before an incident, or whether something similar has happened
    before. Returns a grounded answer followed by the source documents it used
    (in the order the graph was traversed), so you can cite or read them.
    """
    try:
        # run_query itself is print-free, but its seed-fallback path can print
        # warnings to stdout — which would corrupt the MCP stdio (JSON-RPC)
        # channel. Redirect any stray stdout to stderr to keep it clean.
        with redirect_stdout(sys.stderr):
            result = querier.run_query(question)
    except Exception as e:
        return f"Query failed: {type(e).__name__}: {e}"

    lines = [
        result["answer"],
        "",
        "---",
        f"Intent: {result['intent']} ({result['intent_description']})",
        "Sources (in traversal order):",
    ]
    for i, node in enumerate(result["path"], 1):
        if node["is_seed"]:
            lines.append(f"  {i}. {node['filepath']} (seed)")
        else:
            why = node.get("edge_description") or ""
            lines.append(f"  {i}. {node['filepath']} — {why}")
    return "\n".join(lines)


@mcp.tool()
def get_incident_document(filepath: str) -> str:
    """Return the full text of one CR or RCA document by its path.

    Use the filepath exactly as shown in a query_incidents 'Sources' list
    (e.g. 'CR/20251111-Update-CPD-Routes.md'). Use this to read a source
    document in full after a query surfaces it.
    """
    base = config.DOCUMENTS_DIR.resolve()
    target = (base / filepath).resolve()

    # Path-traversal guard: the resolved path must stay inside the docs dir.
    if not target.is_relative_to(base):
        return f"Refused: '{filepath}' is outside the documents directory."
    if not target.is_file():
        return f"Not found: {filepath}"

    return target.read_text(encoding="utf-8")


if __name__ == "__main__":
    # Default transport is stdio (what local MCP clients expect). For a deployed
    # service we'd switch to streamable-HTTP transport instead.
    mcp.run()
