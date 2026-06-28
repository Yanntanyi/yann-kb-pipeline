"""Relationship eval gold set — the graph's real differentiator vs flat RAG.

Tests the one thing RAG genuinely can't do: connect documents by a *relationship*
(this change CAUSED that incident; this change FIXED it; this incident is the
slow-recovery sibling of that one). Each positive question names:

  - must_connect:      docs that must ALL be retrieved to connect the relationship
  - connecting_doc:    the hard-to-retrieve end (usually the CR/change) to spotlight
  - required_relationship: the specific cross-document connection the answer must state

Negative controls (negative=True) have NO real relationship — the correct answer is
to say the two are unrelated. They test that the system doesn't FABRICATE links.

All links here are grounded in the actual documents (RCAs + the CRs read to verify
them). Quantity is capped by how many *genuine* relationships the corpus contains —
inventing links would make the test less trustworthy, not more.

Run:  python eval_relationships.py   ->   writes eval_relationships.jsonl
"""

import json
from pathlib import Path

# ── RCAs ──
CPD   = "RCA/CPD_Cert_Issue.md"
WEBHK = "RCA/Log Webhook Latency.md"
MCOG  = "RCA/MCOG_bottleneck_diagnostics.md"
ODLM  = "RCA/ODLM_Cert_Issue.md"
PODS  = "RCA/Pods Restarted Incident on Oct 2nd.md"
AUTOC = "RCA/RCA_Automatic_Certificate_Renewal__Incident_2024-12-19_to_12-20.md"
DIGIT = "RCA/RCA_Digital_ResponseTime_Spike_8th_Jan_2026.md"
RESP  = "RCA/Response Time Spikes Incidence.md"
E0050 = "RCA/UPS_0050E_iSSUE_17_Jan_2026.md"
VGW   = "RCA/Voicegateway unable to connect to Assistant.md"
WXO   = "RCA/WxO Certificate Issue.md"
# ── CRs ──
CR_CPD      = "CR/20251111 -Update-CPD-Routes-With-Refreshed-Certs-from-.md"
CR_ODLM     = "CR/Patch ODLM in Prod.md"
CR_ODLM_NP  = "CR/Patch ODLM in Non-Prod.md"
CR_SCALEODLM= "CR/Scale Down ODLM in Prod.md"
CR_CERTMGR  = "CR/Rollout Restart cert-manager in Prod.md"
CR_KAFKA    = "CR/Add user privelage to increase kafka partition and rolling restart incoming webhooks.md"
CR_WEBCPU   = "CR/Increase CPU limits for wa-incoming-webhooks.md"
CR_MCOG     = "CR/Set MCOGs CPU Specs and SpecifyIncrease DB max_connections in PROD to support faster restarts.pdf.md"
CR_NOOBAREPL= "CR/Increase replicas for noobaa endpoint in Prod.md"
CR_MCGRECON = "CR/UPS_Change_Request_Voice_Digital_MultiCloudObjectGateway(MCG)_Reconfiguration_PROD.md"
CR_LBCOOK   = "CR/Configure new Digital load balancer to use cookies.md"
CR_NEWLB    = "CR/New Load Balancer for Virutal Assistant - PROD.md"
CR_CERTDATES= "CR/Change Certificate Renewal Dates to 1 year in Non-Prod.md"
CR_TLSRENEW = "CR/UPS_Change_Request_TLS_Cert_Renewal_PROD-East.md"
CR_ZENMINIO = "CR/20250213 - Zenminio.md"

R = []
def add(id, q, must_connect, connecting_doc, rel, business=False, negative=False):
    R.append(dict(id=id, question=q, must_connect=must_connect,
                  connecting_doc=connecting_doc, required_relationship=rel,
                  business=business, negative=negative))

# ── A. CHANGE → INCIDENT (cause): change shares little vocabulary w/ the incident ──
add("R1", "What change caused the December 2025 digital response-time spikes, and why?",
    [RESP, CR_LBCOOK], CR_LBCOOK,
    "Must connect the Dec 1 digital load-balancer change — switching session persistence to "
    "cookie-based stickiness — to the response-time spikes, stating it caused the latency by "
    "concentrating session/Redis load. Describing the spikes alone does NOT count.")
add("R3", "What caused the November 10 voice outage, and which change request triggered it?",
    [CPD, CR_CPD], CR_CPD,
    "Must connect the CPD route certificate update (the change) to the voice outage: the update "
    "caused dropped calls because the renewed certs were not in the Voice Gateway trust store.")
add("R12", "Which load-balancer change is implicated in the digital response-time spikes, and what did it change?",
    [RESP, CR_NEWLB], CR_NEWLB,
    "Must connect the new Global Load Balancer for the virtual assistant (va.ccca.ups.com) to the "
    "response-time spikes — the LB replacement changed session handling and concentrated traffic.")

# ── B. CHANGE → INCIDENT (fix/remediation) ──
add("R2", "Which change fixed the October 2nd pod-restart incident, and what did it do?",
    [PODS, CR_CERTMGR], CR_CERTMGR,
    "Must connect the Oct 2 pod-restart incident to its fix: a rollout restart of cert-manager "
    "to refresh its cache after it looped certificate re-issuance.")
add("R4", "Which change request remediated the ODLM certificate incident, and how?",
    [ODLM, CR_ODLM], CR_ODLM,
    "Must connect the ODLM incident to the Patch ODLM change, which applied a hotfix CS 4.11 "
    "image to the ODLM operator to stop the erroneous certificate re-issuance.")
add("R4b", "Before the ODLM hotfix was ready, what interim change was made to stop the pod restarts?",
    [ODLM, CR_SCALEODLM], CR_SCALEODLM,
    "Must connect the ODLM incident to the interim mitigation: scaling the ODLM deployment down "
    "to 0 replicas so it stops re-issuing certificates and restarting pods while awaiting the hotfix.")
add("R6", "Which change addressed the MCOG bottleneck, and what did it change?",
    [MCOG, CR_MCOG], CR_MCOG,
    "Must connect the MCOG bottleneck to the Set MCOGs CPU Specs change (adding noobaa CPU "
    "requests/limits and max_connections = 2400 to relieve the gateway bottleneck).")
add("R6b", "Which MCG/NooBaa reconfiguration change was made to improve the object-gateway bottleneck affecting Speech?",
    [MCOG, CR_MCGRECON], CR_MCGRECON,
    "Must connect the MCOG (Multi-Cloud Object Gateway) bottleneck to the MCG reconfiguration "
    "change that tuned NooBaa core/endpoints/backing storage to stabilize Watson Speech workloads.")
add("R6c", "Which change increased NooBaa endpoint replicas to speed up slow speech-pod restarts?",
    [MCOG, CR_NOOBAREPL], CR_NOOBAREPL,
    "Must connect the slow speech-cr STT/TTS pod restarts (the MCOG bottleneck symptom) to the "
    "change increasing noobaa-endpoint pods from 1 to 4 (min 4 / max 8) to speed pod recovery.")
add("R7", "Which change fixed the Log Webhook latency, and what privilege did it grant?",
    [WEBHK, CR_KAFKA], CR_KAFKA,
    "Must connect the Log Webhook latency to its fix: granting wa-kafka-user the privilege to "
    "increase Kafka partitions (the Alter permission), enabling the missing partitions.")
add("R7b", "Besides the Kafka permission, which change cleared the Log Webhook backlog faster?",
    [WEBHK, CR_WEBCPU], CR_WEBCPU,
    "Must connect the Log Webhook latency to the change that raised the CPU limit on "
    "wa-incoming-webhooks to clear the Kafka Message_logs backlog faster.")

# ── C. CHANGE → INCIDENT CLUSTER (preventive responses to a pattern) ──
add("R13", "Which change was made to reduce the risk of certificate-renewal incidents recurring, and how?",
    [CR_CERTDATES], CR_CERTDATES,
    "Must connect the recurring certificate-renewal incidents to the preventive change extending "
    "internal certificate validity to 1 year with auto-renewal one month before expiry, outside peak.")
add("R14", "Which change manually triggered an internal-TLS certificate renewal in a controlled window to avoid a peak-period incident?",
    [CR_TLSRENEW], CR_TLSRENEW,
    "Must connect the certificate-renewal-triggers-restart risk to the change that manually "
    "triggered the internal-TLS-cert renewal during the PROD-East backup window as a proactive measure.")

# ── D. INCIDENT ↔ INCIDENT relationships ──
add("R5", "How are the ODLM pod-restart incident and the MCOG bottleneck related?",
    [ODLM, MCOG], MCOG,
    "Must connect the two: ODLM CAUSES pod restarts, while the MCOG bottleneck makes restarts "
    "slow to recover. Listing them separately without that cause-vs-slow-recovery link does NOT count.")
add("R9", "What role did the MCOG bottleneck play in the December 19-20 pod-restart incident?",
    [AUTOC, MCOG], MCOG,
    "Must connect MCOG to the Dec 19-20 incident as a CONTRIBUTING factor that slowed pod recovery "
    "— not the trigger (the trigger was the certificate renewal).")
add("R15", "How is the October 2nd pod-restart incident related to the December 19-20 pod restarts?",
    [PODS, AUTOC], AUTOC,
    "Must connect them as the same failure family: an automatic certificate renewal triggering "
    "speech/STT-TTS pod restarts. Both stem from cert renewal forcing restarts.")
add("R16", "How is the ODLM incident related to the October 2nd cert-manager pod restarts?",
    [ODLM, PODS], PODS,
    "Must connect them as the same mechanism: a certificate operator (ODLM, and cert-manager on "
    "Oct 2) re-issuing/renewing certificates, which forces the affected pods to restart.")
add("R17", "How is the January 8 digital response-time spike related to the January 15-17 CWSMR0050E incident?",
    [DIGIT, E0050], E0050,
    "Must connect them via the shared condition: both occurred during a Central-cluster offline "
    "backup that pushed all traffic to East, overloading it (API failures / AKAMAI blocking).")
add("R18", "Are the December 1-3 and January 8 digital response-time spikes the same root cause?",
    [RESP, DIGIT], DIGIT,
    "Must state they are DIFFERENT causes: Dec 1-3 was a load-balancer/session change; Jan 8 was "
    "an offline-backup load shift with UPS API failures. Same symptom, different cause.")

# ── E. BIDIRECTIONAL (start from the change, find the incident) ──
add("R3r", "What production incident did the November 11 CPD route certificate update cause?",
    [CR_CPD, CPD], CPD,
    "Must connect the CPD route certificate update to the November 10 voice outage it caused "
    "(dropped calls due to the Voice Gateway trust store not having the renewed certs).")
add("R2r", "What incident did the cert-manager rollout restart in Prod resolve?",
    [CR_CERTMGR, PODS], PODS,
    "Must connect the cert-manager rollout restart to the October 2nd pod-restart incident it resolved.")

# ── F. MULTI-HOP (3-document chain) ──
add("R19", "Trace the December 19-20 incident from its trigger, through why recovery was slow, to a change made to address that slowness.",
    [AUTOC, MCOG, CR_MCGRECON], CR_MCGRECON,
    "Must connect three docs: the cert renewal (trigger) → the MCOG bottleneck (why recovery was "
    "slow) → an MCG/NooBaa reconfiguration change made to relieve that bottleneck.")
add("R8", "Which of our own change requests directly caused production incidents?",
    [CR_CPD, CPD, CR_LBCOOK, RESP], CR_LBCOOK,
    "Must connect specific changes to the incidents they caused: the CPD route cert update caused "
    "the Nov 10 voice outage, AND the Dec 1 digital load-balancer (cookie) change caused the spikes.",
    business=True)
add("R20", "Across the certificate incidents, which changes were remediations versus preventive measures?",
    [CR_ODLM, CR_CERTMGR, CR_CERTDATES], CR_CERTDATES,
    "Must connect remediation changes (Patch ODLM, cert-manager restart) to the incidents they "
    "fixed, AND preventive changes (1-year cert renewal, pre-peak TLS renewal) to the recurring "
    "cert-renewal risk they were meant to reduce.", business=True)

# ── G. NEGATIVE CONTROLS (no real link — must NOT fabricate one) ──
add("N_R1", "Did the Log Webhook latency incident cause the November 10 CPD voice outage?",
    [WEBHK, CPD], CPD,
    "Must state there is NO connection: the Log Webhook latency (a Kafka partition/permission "
    "issue) and the CPD voice outage (a certificate trust-store issue) are unrelated incidents.",
    negative=True)
add("N_R2", "Is the zen-minio memory-limit increase connected to the ODLM pod-restart incident?",
    [CR_ZENMINIO, ODLM], ODLM,
    "Must state there is NO documented connection: the zen-minio memory change and the ODLM "
    "certificate-re-issuance incident are unrelated.", negative=True)
add("N_R3", "Did the WxO (Watson Orchestrate) certificate issue cause an outage on the Voice channel?",
    [WXO, VGW], VGW,
    "Must state there is NO connection: the WxO certificate issue was a non-prod Orchestrate "
    "deployment failure and did not cause the Voice Gateway / voice-channel outage.", negative=True)
add("N_R4", "Was the January 8 digital response-time spike caused by a certificate renewal?",
    [DIGIT], DIGIT,
    "Must state NO: the Jan 8 spike was caused by a Central offline backup shifting load to East "
    "and UPS API failures — not a certificate renewal.", negative=True)
add("N_R5", "Did the MCOG bottleneck cause the Voice Gateway DNS connectivity outage on October 14?",
    [MCOG, VGW], VGW,
    "Must state NO: the Oct 14 outage was caused by an engineer disabling the private DNS zone; "
    "it is unrelated to the MCOG bottleneck.", negative=True)


def main():
    out = Path(__file__).with_name("eval_relationships.jsonl")
    with out.open("w", encoding="utf-8") as f:
        for r in R:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    pos = sum(1 for r in R if not r["negative"])
    neg = sum(1 for r in R if r["negative"])
    biz = sum(1 for r in R if r["business"])
    print(f"Wrote {len(R)} relationship questions -> {out}")
    print(f"  positive links: {pos}   negative controls: {neg}   business-tagged: {biz}")


if __name__ == "__main__":
    main()
