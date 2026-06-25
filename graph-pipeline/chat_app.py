"""chat_app.py — A small web chat UI for querying the knowledge graph.

Wraps the existing KnowledgeGraphQuerier (ask.py) behind a Flask server and a
single self-contained HTML page. Every answer comes with a "trace" showing how
the system got there: the detected intent, the edge plan, the seed documents,
and the path it walked (each hop labeled with the edge description that
justified it).

Run it on the machine that can reach Neo4j, Elasticsearch, and the LLM:
  pip install flask
  python3 chat_app.py
then open http://127.0.0.1:8000 in a browser.
"""

from flask import Flask, request, jsonify, Response

from ask import KnowledgeGraphQuerier

app = Flask(__name__)

# One shared querier for the whole server. Its Neo4j driver and LLM client are
# thread-safe, so concurrent requests are fine. Built once at startup so we
# don't reconnect on every question.
querier = KnowledgeGraphQuerier()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UPS Watson — Incident Intelligence</title>
<style>
  :root {
    --bg: #0e1117; --panel: #161b22; --panel2: #1c2230; --border: #2b3240;
    --text: #e6edf3; --muted: #8b949e; --accent: #4493f8; --seed: #3fb950;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
  }
  header {
    padding: 14px 20px; border-bottom: 1px solid var(--border);
    background: var(--panel); display: flex; align-items: baseline; gap: 12px;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header span { color: var(--muted); font-size: 13px; }
  #chat {
    flex: 1; overflow-y: auto; padding: 24px;
    display: flex; flex-direction: column; gap: 18px;
  }
  .msg { max-width: 820px; width: 100%; margin: 0 auto; }
  .role { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .bubble {
    padding: 14px 16px; border-radius: 10px; border: 1px solid var(--border);
    white-space: pre-wrap; line-height: 1.5; word-wrap: break-word;
  }
  .user .bubble { background: var(--panel2); border-color: #30507a; }
  .assistant .bubble { background: var(--panel); }
  .thinking { color: var(--muted); font-style: italic; }
  .error .bubble { background: #2d1618; border-color: #6e2c30; color: #ffb4b4; }
  details.trace {
    margin-top: 12px; background: var(--panel2); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 12px; font-size: 13px;
  }
  details.trace summary { cursor: pointer; color: var(--accent); user-select: none; }
  .trace-section { margin: 10px 0 4px; color: var(--muted); font-size: 12px;
    text-transform: uppercase; letter-spacing: .04em; }
  .pill {
    display: inline-block; background: #1f6feb33; color: #79c0ff;
    border: 1px solid #1f6feb55; padding: 1px 8px; border-radius: 999px;
    font-size: 12px; margin-right: 6px;
  }
  .path-item { padding: 6px 0; border-top: 1px solid var(--border); }
  .path-item:first-child { border-top: none; }
  .path-file { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
  .path-seed { color: var(--seed); }
  .path-why { color: var(--muted); font-size: 12px; margin-top: 3px; font-style: italic; }
  .bubble code { background: rgba(127,127,127,.15); padding: 1px 4px; border-radius: 3px; font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; }
  .bubble ul, .bubble ol { margin: 6px 0 6px 20px; padding: 0; }
  .bubble li { margin: 2px 0; }
  footer {
    border-top: 1px solid var(--border); background: var(--panel);
    padding: 14px 20px;
  }
  .composer { max-width: 820px; margin: 0 auto; display: flex; gap: 10px; }
  #q {
    flex: 1; resize: none; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px;
    font-size: 14px; font-family: inherit; line-height: 1.4; max-height: 160px;
  }
  #q:focus { outline: none; border-color: var(--accent); }
  #send {
    background: var(--accent); color: #fff; border: none; border-radius: 10px;
    padding: 0 20px; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  #send:disabled { opacity: .5; cursor: default; }
  .hint { max-width: 820px; margin: 8px auto 0; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
  <header>
    <h1>UPS Watson — Incident Intelligence</h1>
    <span>Ask about incidents, changes, root causes, or timelines</span>
  </header>
  <div id="chat"></div>
  <footer>
    <div class="composer">
      <textarea id="q" rows="1" placeholder="e.g. What caused the CPD certificate outage?"></textarea>
      <button id="send">Ask</button>
    </div>
    <div class="hint">Enter to send · Shift+Enter for a new line · each answer includes a "How I got this" trace</div>
  </footer>

<script>
  const chat = document.getElementById("chat");
  const input = document.getElementById("q");
  const send = document.getElementById("send");

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  // Render a small, safe subset of Markdown. Input MUST already be HTML-escaped;
  // we only re-introduce the specific tags we generate here, so model output can't
  // inject HTML. Handles **bold**, `code`, #-headers, and -/* and 1. lists.
  function mdToHtml(escaped) {
    const inline = (t) => t
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    let html = "", list = null;
    const closeList = () => { if (list) { html += "</" + list + ">"; list = null; } };
    for (const raw of escaped.split(/\r?\n/)) {
      const line = raw.trim();
      let m;
      if (!line) { closeList(); continue; }
      if (m = line.match(/^#{1,6}\s+(.*)$/)) {
        closeList(); html += "<strong>" + inline(m[1]) + "</strong><br>";
      } else if (m = line.match(/^[-*]\s+(.*)$/)) {
        if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
        html += "<li>" + inline(m[1]) + "</li>";
      } else if (m = line.match(/^\d+\.\s+(.*)$/)) {
        if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
        html += "<li>" + inline(m[1]) + "</li>";
      } else {
        closeList(); html += inline(line) + "<br>";
      }
    }
    closeList();
    return html;
  }

  function addMessage(role, html) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + role;
    const label = role === "user" ? "You" : (role === "error" ? "Error" : "Watson");
    wrap.innerHTML = '<div class="role">' + label + '</div><div class="bubble">' + html + '</div>';
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
    return wrap;
  }

  function renderTrace(d) {
    let h = '<details class="trace"><summary>How I got this</summary>';
    h += '<div class="trace-section">Intent</div>';
    h += '<span class="pill">' + esc(d.intent) + '</span>' + esc(d.intent_description);
    h += '<div class="trace-section">Edge plan (followed in this order)</div>';
    h += esc((d.edge_priority || []).join("  →  "));
    h += '<div class="trace-section">Seed documents (' + (d.seeds || []).length + ')</div>';
    (d.seeds || []).forEach(function (s) {
      h += '<div class="path-file">🌱 ' + esc(s) + '</div>';
    });
    h += '<div class="trace-section">Traversal path (' + (d.path || []).length + ' docs)</div>';
    (d.path || []).forEach(function (n, i) {
      h += '<div class="path-item">';
      if (n.is_seed) {
        h += '<div class="path-file path-seed">seed · ' + esc(n.filepath) + '</div>';
      } else {
        h += '<div class="path-file">hop ' + i + ' · ' + esc(n.filepath) + '</div>';
        if (n.edge_description) h += '<div class="path-why">why: ' + esc(n.edge_description) + '</div>';
      }
      h += '</div>';
    });
    h += '</details>';
    return h;
  }

  async function ask() {
    const query = input.value.trim();
    if (!query) return;
    input.value = "";
    input.style.height = "auto";
    send.disabled = true;

    addMessage("user", esc(query));
    const pending = addMessage("assistant", '<span class="thinking">Thinking… (classifying, retrieving seeds, walking the graph, writing the answer)</span>');

    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query })
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        pending.className = "msg error";
        pending.querySelector(".bubble").innerHTML = esc(data.error || ("HTTP " + res.status));
      } else {
        pending.querySelector(".bubble").innerHTML = mdToHtml(esc(data.answer)) + renderTrace(data);
      }
    } catch (e) {
      pending.className = "msg error";
      pending.querySelector(".bubble").innerHTML = esc("Request failed: " + e);
    } finally {
      send.disabled = false;
      chat.scrollTop = chat.scrollHeight;
      input.focus();
    }
  }

  send.addEventListener("click", ask);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
  });
  // auto-grow the textarea
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });
  input.focus();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/api/ask", methods=["POST"])
def api_ask():
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Empty query."}), 400
    try:
        return jsonify(querier.run_query(query))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


if __name__ == "__main__":
    print("\nUPS Watson chat UI → http://127.0.0.1:8000  (Ctrl+C to stop)\n")
    # threaded=True lets multiple questions run at once; debug=False avoids the
    # reloader spinning up a second querier (and a second set of connections).
    app.run(host="127.0.0.1", port=8000, threaded=True, debug=False)
