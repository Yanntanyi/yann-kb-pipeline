orchestrate toolkits add --kind mcp --name ups_watson \
  --description "UPS incident knowledge graph" \
  --url "https://unpopular-empathic-amenity.ngrok-free.dev/mcp" \
  --transport "streamable_http" \
  --tools "*"


orchestrate tools list
orchestrate agents import -f ../orchestrate/incident_intelligence_agent.yaml
