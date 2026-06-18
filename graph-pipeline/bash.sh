python3 - <<'PY'
import json, requests
from llm_client import WatsonxClient
c = WatsonxClient()
tok = c._get_token()
r = requests.post(
    f"{c.base_url}/ml/v1/text/chat?version={c.api_version}",
    headers={"Authorization": f"Bearer {tok}",
             "Content-Type": "application/json", "Accept": "application/json"},
    json={
        "model_id": c.model,
        "project_id": c.project_id,
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 300,
        "temperature": 0.1,
    },
    timeout=300,
)
print("STATUS", r.status_code)
print(json.dumps(r.json(), indent=2)[:4000])
PY
