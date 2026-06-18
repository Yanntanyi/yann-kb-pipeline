# Surfacing the incident knowledge graph through watsonx Orchestrate

This wraps our MCP server (`graph-pipeline/mcp_server.py`) in a watsonx Orchestrate
**agent**, so UPS users can chat with the incident knowledge graph inside
Orchestrate. The agent uses our MCP server as a **toolkit** — the same server Bob
already connects to. Nothing about the graph or `run_query()` changes.

Docs: https://developer.watson-orchestrate.ibm.com/

---

## The pieces

- **MCP server** (`graph-pipeline/mcp_server.py`) — exposes `query_incidents` and
  `get_incident_document`. This is the *tool*.
- **Agent** (`incident_intelligence_agent.yaml`) — the AI agent users talk to. It
  *uses* the tools. This is the *consumer*.

---

## One reality check first: where does the MCP server run?

The agent's LLM decides to call `query_incidents`; Orchestrate then runs our MCP
server to fulfill it. That server still needs to reach **Neo4j**, **Elasticsearch**,
and **watsonx** — exactly as it does for Bob. So:

- **Local testing (recommended first):** run Orchestrate's local **Developer Edition**
  so the MCP server runs on this machine and reaches your local Neo4j/ES, just like
  the Bob setup. (Check the docs for the developer-edition / local server command.)
- **Cloud Orchestrate:** the server would run in Orchestrate's runtime, which
  *cannot* see your laptop's `localhost` Neo4j/ES. That's the deployment step
  (the "Project Orion Runtime" in the diagram) — defer it until we containerize
  Neo4j/ES/MCP into a reachable cluster.

Start local; it mirrors what already works with Bob.

---

## Steps

### 1. Install the ADK (Python 3.11+)
```bash
pip install --upgrade ibm-watsonx-orchestrate
```

### 2. Connect + activate your environment
```bash
orchestrate env add -n ups -u <your-service-instance-url>
orchestrate env activate ups        # prompts for your API key
```

### 3. Import our MCP server as a toolkit
Use the venv Python (so `mcp` + our deps load) and an absolute package root:
```bash
orchestrate toolkits add \
  --kind mcp \
  --name ups_watson \
  --description "UPS incident knowledge graph query tools" \
  --package-root /Users/yanntanyi/Desktop/Yann-UPS-WXO/UPS-Watson-Knowledge-Base/graph-pipeline \
  --command "/Users/yanntanyi/Desktop/Yann-UPS-WXO/UPS-Watson-Knowledge-Base/graph-pipeline/venv/bin/python mcp_server.py" \
  --tools "*"
```
Notes:
- Verify exact flag spelling with `orchestrate toolkits add --help` (docs show
  `--package-root`; some builds use `--package_root`).
- The MCP server needs `WATSONX_API_KEY` / `WATSONX_PROJECT_ID` in its environment.
  The clean way is an Orchestrate **connection** referenced via `--app-id`
  (see the connections docs); for quick local testing, make sure those vars are
  exported in the shell that launches the server.
- Confirm the tool names it registered: `orchestrate tools list`.

### 4. Import the agent
```bash
orchestrate agents import -f incident_intelligence_agent.yaml
```
(If the tool names from step 3 are prefixed, update the `tools:` list in the YAML
to match, then re-import.)

### 5. Talk to it
Open the agent in the Orchestrate chat UI (or `orchestrate chat start` if your
instance supports the local chat), and ask things like:
- "What caused the CPD certificate outage?"
- "How was the ODLM pod restart resolved?"

---

## To confirm against your instance / the docs

- The local Developer Edition command (for end-to-end local testing).
- Exact `orchestrate agents import` flags.
- How secrets are wired to the MCP server (connection via `--app-id`).
- Whether MCP tool names get namespaced by the toolkit.
