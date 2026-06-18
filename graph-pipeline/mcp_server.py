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

import os
import sys
import asyncio
from contextlib import redirect_stdout

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

import config
from ask import KnowledgeGraphQuerier

# Build the querier once at startup — connects to Neo4j, Elasticsearch, and the
# LLM, exactly like the CLI and web UI do.
querier = KnowledgeGraphQuerier()

# Create MCP server
server = Server("ups-watson-graph")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="query_incidents",
            description=(
                "Answer a question about UPS cloud incidents using the knowledge graph. "
                "Covers change requests (CRs) and root cause analyses (RCAs). Use this for "
                "questions about what caused an outage, how an issue was resolved, what "
                "changed before an incident, or whether something similar has happened "
                "before. Returns a grounded answer followed by the source documents it used "
                "(in the order the graph was traversed), so you can cite or read them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask about incidents"
                    }
                },
                "required": ["question"]
            },
        ),
        Tool(
            name="get_incident_document",
            description=(
                "Return the full text of one CR or RCA document by its path. "
                "Use the filepath exactly as shown in a query_incidents 'Sources' list "
                "(e.g. 'CR/20251111-Update-CPD-Routes.md'). Use this to read a source "
                "document in full after a query surfaces it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the document (e.g. 'CR/20251111-Update-CPD-Routes.md')"
                    }
                },
                "required": ["filepath"]
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    if name == "query_incidents":
        question = arguments.get("question", "")
        try:
            # run_query itself is print-free, but its seed-fallback path can print
            # warnings to stdout — which would corrupt the MCP stdio (JSON-RPC)
            # channel. Redirect any stray stdout to stderr to keep it clean.
            with redirect_stdout(sys.stderr):
                result = querier.run_query(question)
        except Exception as e:
            return [TextContent(
                type="text",
                text=f"Query failed: {type(e).__name__}: {e}"
            )]

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

        return [TextContent(type="text", text="\n".join(lines))]

    elif name == "get_incident_document":
        filepath = arguments.get("filepath", "")
        base = config.DOCUMENTS_DIR.resolve()
        target = (base / filepath).resolve()

        # Path-traversal guard: the resolved path must stay inside the docs dir.
        if not target.is_relative_to(base):
            return [TextContent(
                type="text",
                text=f"Refused: '{filepath}' is outside the documents directory."
            )]
        if not target.is_file():
            return [TextContent(
                type="text",
                text=f"Not found: {filepath}"
            )]

        content = target.read_text(encoding="utf-8")
        return [TextContent(type="text", text=content)]

    else:
        return [TextContent(
            type="text",
            text=f"Unknown tool: {name}"
        )]


async def run_stdio():
    """Run over stdio — how a LOCAL client (Bob / Claude Code) launches us as a
    subprocess. This is the default and what already works with Bob.
    """
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


def run_http(host: str, port: int):
    """Run as a streamable-HTTP web service — how a REMOTE client (cloud watsonx
    Orchestrate) reaches us, over a URL. Exposes the MCP endpoint at /mcp.

    Imports are local so the stdio path (Bob) never needs the web stack. mcp
    already pulls in starlette + uvicorn as dependencies.
    """
    import contextlib

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    # stateless=True: each request is self-contained (no server-side session to
    # keep), which is what we want behind a tunnel / load balancer.
    session_manager = StreamableHTTPSessionManager(
        app=server, json_response=False, stateless=True
    )

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)
    print(f"MCP streamable-http server on http://{host}:{port}/mcp", file=sys.stderr)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    # MCP_TRANSPORT=http runs the web service (for Orchestrate); anything else
    # (the default) runs stdio (for Bob). Lets one file serve both clients.
    if os.environ.get("MCP_TRANSPORT", "stdio").lower() == "http":
        run_http(
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8080")),
        )
    else:
        asyncio.run(run_stdio())
