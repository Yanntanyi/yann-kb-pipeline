# UPS Watson Incident Intelligence: Deployment, Explained Simply

*Right now our system only runs on a laptop. This doc explains, in plain words, how we get it running "out there" so a cloud watsonx Orchestrate agent can use it — and explains the scary words (containers, Kubernetes, OpenShift) along the way.*

---

## 1. The whole problem, in one breath

Cloud watsonx Orchestrate lives on the internet (in IBM Cloud). Our Neo4j and Elasticsearch live on a laptop. **The cloud can't reach your laptop.** That's the entire problem.

So everything in this doc is about one goal: **move our system off the laptop and onto a server on the internet, so the cloud can reach it.** That's it. Don't let the jargon below make it feel bigger than that.

---

## 2. What "containerizing" means (plain words)

A **container** is a **sealed lunchbox for an app**. You pack the code *plus everything it needs* — the right Python, all the libraries, the settings — into one standard box. Then that box runs the same way on *any* computer, not just your laptop.

- **Why we need it:** our app only works on your laptop today because Python and all the libraries are set up just right there. A container lets us copy that exact working setup and run it anywhere.
- **The analogy:** shipping containers. Once cargo got packed into standard steel boxes, any ship, truck, or crane could handle it. Software containers do the same for apps — a standard box any server can run.
- **The tool:** Docker. Free. The box you build is called an **image**; a running copy of it is a **container**.

We have three things to put in boxes: the **Query/MCP service**, **Neo4j**, and **Elasticsearch**.

---

## 3. What Kubernetes and OpenShift are (plain words)

Once you have apps in containers, a new question appears: *who keeps them running?* If a container crashes at 3am, who restarts it? If lots of users show up, who starts more copies? If the database box needs to find the query box, who connects them? Doing all that by hand across servers is a nightmare.

**Kubernetes is the manager that does all of that for you.** You tell it your goals — *"keep 3 copies of the query service running, connect it to the database, restart anything that dies, and expose it to the internet here"* — and Kubernetes makes it true and *keeps* it true.

Two analogies that help:
- **A conductor:** your containers are musicians; Kubernetes is the conductor keeping them in sync, and if a musician stops playing, it instantly swaps in a replacement.
- **An automated shipping port:** containers are the steel boxes; Kubernetes is the whole port — cranes, scheduling, tracking — deciding where each box goes and replacing lost ones, all automatically.

**OpenShift** is simply **IBM/Red Hat's enterprise version of Kubernetes** — the same thing, plus extra security, tools, and a friendlier dashboard. If we deploy onto IBM's infrastructure, it'll most likely be OpenShift. *Wherever this doc says "Kubernetes," read "Kubernetes or OpenShift."*

> ⚠️ **Confusing name alert:** Kubernetes "orchestrates" containers, and the IBM product is called watsonx "Orchestrate." **They are completely different things.** Kubernetes orchestrates *containers*; watsonx Orchestrate orchestrates *AI agents*. Same word, unrelated.

**In our project:** Kubernetes/OpenShift is the home for the "Project Orion Runtime" — it would run our three containers (plus the ingestion job), keep them alive, wire them together, and expose the query service to the internet so cloud Orchestrate can reach it. **You do not need Kubernetes to start** (see the plan in §8) — it's the "do it properly for production" step, not the first step.

---

## 4. The picture

```
                    IBM Cloud
 ┌─────────────────────────────────────────┐
 │ watsonx Orchestrate (your cloud instance) │
 │   incident_intelligence agent             │
 │      → uses a REMOTE MCP toolkit          │
 └──────────────────┬────────────────────────┘
                    │  HTTPS (a normal secure web request)
                    ▼
┌────────── the server / cluster (on the internet) ──────────┐
│  Front door (web address + lock)                            │
│            │                                                │
│            ▼                                                │
│   Query + MCP service   (our mcp_server.py, as a website)   │
│        │                  │                                 │
│        ▼                  ▼                                 │
│     Neo4j            Elasticsearch    (the two databases)   │
│        ▲                  ▲                                 │
│        └──── Ingestion (the graph builder, run on demand) ──│
│                                                             │
│   Secrets: the watsonx keys + Neo4j password (kept safe)    │
└────────────────────────┬────────────────────────────────────┘
                        │  (calls out to)
                        ▼
              watsonx.ai  (the gpt-oss model + embeddings)
```

---

## 5. What each box is (and why it's there)

- **Query + MCP service** — this is our `mcp_server.py`, but running as a little **website** instead of a local program, so the cloud can reach it at a web address. It's the *only* box open to the outside. When the agent asks a question, this box does the work (finds seeds, walks the graph, writes the answer) and sends it back.
- **Neo4j** — the graph (the knowledge). It holds important data, so it needs **permanent storage** (so nothing is lost on a restart). It stays **private** — only the Query service can talk to it, never the internet.
- **Elasticsearch** — the search index used to find the starting documents. Same as Neo4j: permanent storage, private.
- **Ingestion** — the pipeline that *builds* the graph (the 5 phases + indexing). It's a **run-it-when-you-need-it** job, not an always-on service, because rebuilding is heavy work and shouldn't slow down live questions.
- **Secrets** — the watsonx API key, project id, and Neo4j password, kept in a secure store (never written into the code or the boxes).
- **watsonx.ai** — the actual AI model, which stays where it is (IBM's cloud). Our Query service calls out to it.

---

## 6. How a question flows (start to finish)

1. A user asks the Orchestrate agent: *"What caused the cert outage?"*
2. The agent decides to use our tool and sends a secure web request to our **Query service**.
3. The Query service finds the starting documents (Elasticsearch), walks the graph (Neo4j), and asks the AI model (watsonx) to write the answer.
4. The answer goes back to the agent, and the agent replies to the user.

When new incident documents arrive, we run the **Ingestion** job once; it updates Neo4j and Elasticsearch, and the Query service automatically uses the fresh data.

---

## 7. The one change to our code

Today `mcp_server.py` runs as a **local program** that Bob launches (this is "stdio" mode). To live on a server, it needs to run as a **website** instead. That's a one-line change:

```python
mcp.run(transport="streamable-http")   # run as a web service instead of a local program
```

Nothing about the graph, the tools, or `run_query()` changes — we just change *how it's reached*, from "a program on your machine" to "a web address." We'll keep the old local mode too, so Bob still works.

---

## 8. Will this cost money?

- **Building the containers = free.** Docker is free, and running them on your own laptop is free.
- **Running them on a server the cloud can reach = normally costs money**, because that needs an always-on machine with a public address.

But you shouldn't pay personally. Two options:
1. **Proper path — use IBM's resources.** You're an intern; ask your team for access to IBM Cloud / OpenShift. IBM covers the cost.
2. **Free testing trick — a tunnel.** Keep the containers on your laptop and use a free tool (`ngrok` or Cloudflare Tunnel) that gives your laptop a temporary public web address. Cloud Orchestrate can then reach your laptop through it — no paid server. Great for proving it works; not for production.

---

## 9. The plan (smallest steps first)

1. **Step 1 — just make it reachable (cheap/free).** Put the three pieces in containers, run them together (on your laptop, or a small host), and give the Query service a public address (a free tunnel, or a small IBM host). Connect cloud Orchestrate to it as a *remote* MCP tool. **Goal: see the agent answer a real question through the cloud.**
2. **Step 2 — do it properly on Kubernetes/OpenShift.** Move the containers onto a real cluster with permanent storage, automatic restarts, and proper security. This is the production version of the picture above.
3. **Step 3 — automate the graph updates.** Make ingestion run on a schedule (or a trigger) so new incident docs flow in without anyone running it by hand.

Step 1 is the fast win that gets the demo working; Steps 2–3 make it solid and hands-off.

---

## 10. Things we still need to decide (with your team)

1. **Where will it run?** IBM Cloud OpenShift? A small IBM cloud VM? (For Step 1 we can dodge this with the free tunnel.)
2. **Run our own Neo4j/Elasticsearch in containers, or use ready-made cloud versions?** Ready-made = less work; our own = more control.
3. **How do the incident documents get to the ingestion job?** (Packed in the box, pulled from git, etc.)
4. **What "lock" (login) does cloud Orchestrate use to call our service securely?** (Confirm with the remote-MCP-toolkit docs.)

---

*Built for UPS Watson Incident Intelligence.*
