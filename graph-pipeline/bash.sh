git pull
python3 -c "from llm_client import get_llm_client; c=get_llm_client(); print(type(c).__name__); print(c.generate_text('Say OK')); print(c.generate_json('Return only JSON: {\"ok\": true}')); print('embed dim:', len(c.embed(['hello world'])[0]))"
