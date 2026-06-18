orchestrate toolkits add --kind mcp --name ups_watson \
  --description "UPS incident knowledge graph" \
  --url "https://unpopular-empathic-amenity.ngrok-free.dev/mcp" \
  --transport "streamable_http" \
  --tools "*"


orchestrate tools list
orchestrate agents import -f ../orchestrate/incident_intelligence_agent.yaml


Questions to test it (organized by what they exercise)
These hit the four different traversal behaviors plus the edge cases. Adjust names to match your actual corpus — and use whatever filenames the agent cites in its "Sources" to test the document-fetch tool.

Causal (walks the cause/time chain):

"What caused the CPD certificate outage?"
"Why did the pods restart on October 2nd?"
Resolution (finds the action taken):

"How was the ODLM pod restart issue resolved?"
"What was done to fix the SMS communication issues?"
Timeline (orders events in time):

"What changed in the week before the November voice incident?"
"What changes happened around the CPD certificate renewal?"
Similar (finds related past cases):

"Has a memory-pressure pod restart happened before?"
"Have there been other certificate-related incidents?"
The second tool (get_incident_document) — after any answer, pick a filename it cited:

"Show me the full text of CR/20251111-Update-CPD-Routes.md."
Grounding / does-it-hallucinate test (it should decline or say it's not in the docs):

"What's the capital of France?"
"What's our AWS bill this month?"
