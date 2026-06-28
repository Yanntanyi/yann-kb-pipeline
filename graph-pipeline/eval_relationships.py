"""Relationship eval gold set — the graph's real differentiator vs flat RAG.

This tests the one thing RAG genuinely can't do: connect two documents by a
*relationship* (this change CAUSED that incident; this incident is the slow-
recovery sibling of that one). Each question names:

  - must_connect:      the documents that must ALL be retrieved to connect them
  - connecting_doc:    the *hard-to-retrieve* one to spotlight — usually the
                       CR/change, which often shares little vocabulary with the
                       incident, so flat similarity search misses it
  - required_relationship: the specific cross-document connection the answer must
                       state (not just facts about each doc)

The runner scores two axes per system: did it RETRIEVE the connecting doc, and
did the ANSWER STATE the relationship. The graph's advantage shows up where the
connecting doc has low vocabulary overlap (flat RAG can't connect what it never
retrieved).

Run:  python eval_relationships.py   ->   writes eval_relationships.jsonl
"""

import json
from pathlib import Path

CPD   = "RCA/CPD_Cert_Issue.md"
WEBHK = "RCA/Log Webhook Latency.md"
MCOG  = "RCA/MCOG_bottleneck_diagnostics.md"
ODLM  = "RCA/ODLM_Cert_Issue.md"
PODS  = "RCA/Pods Restarted Incident on Oct 2nd.md"
AUTOC = "RCA/RCA_Automatic_Certificate_Renewal__Incident_2024-12-19_to_12-20.md"
RESP  = "RCA/Response Time Spikes Incidence.md"
CR_CPD     = "CR/20251111 -Update-CPD-Routes-With-Refreshed-Certs-from-.md"
CR_ODLM    = "CR/Patch ODLM in Prod.md"
CR_CERTMGR = "CR/Rollout Restart cert-manager in Prod.md"
CR_KAFKA   = "CR/Add user privelage to increase kafka partition and rolling restart incoming webhooks.md"
CR_MCOG    = "CR/Set MCOGs CPU Specs and SpecifyIncrease DB max_connections in PROD to support faster restarts.pdf.md"
CR_LBCOOK  = "CR/Configure new Digital load balancer to use cookies.md"

R = []
def add(id, q, must_connect, connecting_doc, rel, business=False):
    R.append(dict(id=id, question=q, must_connect=must_connect,
                  connecting_doc=connecting_doc, required_relationship=rel,
                  business=business))

# ── Strong discriminators: the change shares little vocabulary with the incident ──
add("R1", "What change caused the December 2025 digital response-time spikes, and why?",
    [RESP, CR_LBCOOK], CR_LBCOOK,
    "Must connect the Dec 1 digital load-balancer change — switching session persistence to "
    "cookie-based stickiness — to the response-time spikes, stating that change caused the "
    "latency by concentrating session/Redis load. Describing the spikes alone does NOT count.")
add("R2", "Which change fixed the October 2nd pod-restart incident, and what did it do?",
    [PODS, CR_CERTMGR], CR_CERTMGR,
    "Must connect the Oct 2 pod-restart incident to its remediation: a rollout restart of "
    "cert-manager to refresh its cache after it looped certificate re-issuance. Must name the "
    "cert-manager restart as the fix for THAT incident.")
add("R3", "What caused the November 10 voice outage, and which change request triggered it?",
    [CPD, CR_CPD], CR_CPD,
    "Must connect the CPD route certificate update (the change) to the voice outage, stating "
    "the update caused dropped calls because the renewed certs were not in the Voice Gateway "
    "trust store. Describing the outage without naming the triggering change does NOT count.")
add("R8", "Which of our own change requests directly caused production incidents?",
    [CR_CPD, CPD, CR_LBCOOK, RESP], CR_LBCOOK,
    "Must connect specific changes to the incidents they caused: the CPD route cert update "
    "caused the Nov 10 voice outage, AND the Dec 1 digital load-balancer (cookie) change "
    "caused the response-time spikes.", business=True)

# ── Fix-link questions: the remediating CR for an incident ──
add("R4", "Which change request remediated the ODLM certificate incident, and how?",
    [ODLM, CR_ODLM], CR_ODLM,
    "Must connect the ODLM incident to the Patch ODLM change, which applied a hotfix CS 4.11 "
    "image to the ODLM operator to stop the erroneous certificate re-issuance.")
add("R6", "Which change addressed the MCOG bottleneck, and what did it change?",
    [MCOG, CR_MCOG], CR_MCOG,
    "Must connect the MCOG bottleneck to the Set MCOGs CPU Specs change (adding noobaa CPU "
    "requests/limits and max_connections = 2400).")
add("R7", "Which change fixed the Log Webhook latency, and what privilege did it grant?",
    [WEBHK, CR_KAFKA], CR_KAFKA,
    "Must connect the Log Webhook latency to its fix: granting wa-kafka-user the privilege to "
    "increase Kafka partitions (the Alter permission), enabling the missing partitions.")

# ── Incident-to-incident relationships (no CR; tests stating the link) ──
add("R5", "How are the ODLM pod-restart incident and the MCOG bottleneck related?",
    [ODLM, MCOG], MCOG,
    "Must connect the two incidents: ODLM CAUSES pod restarts, while the MCOG bottleneck makes "
    "restarts slow to recover. Listing them separately without the cause-vs-slow-recovery "
    "relationship does NOT count.")
add("R9", "What role did the MCOG bottleneck play in the December 19-20 pod-restart incident?",
    [AUTOC, MCOG], MCOG,
    "Must connect MCOG to the Dec 19-20 incident as a CONTRIBUTING factor that slowed pod "
    "recovery — not the trigger (the trigger was the certificate renewal).")


def main():
    out = Path(__file__).with_name("eval_relationships.jsonl")
    with out.open("w", encoding="utf-8") as f:
        for r in R:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {len(R)} relationship questions -> {out}")
    print(f"  business-tagged (change accountability): {sum(1 for r in R if r['business'])}")


if __name__ == "__main__":
    main()
