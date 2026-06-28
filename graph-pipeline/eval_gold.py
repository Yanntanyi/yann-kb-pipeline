"""Structured gold set for the metrics harness — emits eval_gold.jsonl.

This is the machine-readable encoding of docs/Evaluation-Questions.md (answers
already verified against the source documents). Each row carries:
  - intents: acceptable intent label(s); the classifier is "correct" if it picks
    any one of them (some questions are genuinely answerable by more than one).
  - must_have / nice_to_have: required / supporting document ids (filepaths
    relative to docs/), for retrieval recall & precision.
  - key_facts: atomic facts a correct answer must contain (for the LLM judge).
  - must_abstain: true if the corpus cannot answer it / the doc says "unknown".

Run:  python eval_gold.py   ->   writes eval_gold.jsonl
"""

import json
from pathlib import Path

# ── doc id aliases ────────────────────────────────────────────────────────────
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
CR_CPD     = "CR/20251111 -Update-CPD-Routes-With-Refreshed-Certs-from-.md"
CR_ODLM    = "CR/Patch ODLM in Prod.md"
CR_CERTMGR = "CR/Rollout Restart cert-manager in Prod.md"
CR_KAFKA   = "CR/Add user privelage to increase kafka partition and rolling restart incoming webhooks.md"
CR_WEBCPU  = "CR/Increase CPU limits for wa-incoming-webhooks.md"
CR_MCOG    = "CR/Set MCOGs CPU Specs and SpecifyIncrease DB max_connections in PROD to support faster restarts.pdf.md"
CR_LBCOOK  = "CR/Configure new Digital load balancer to use cookies.md"

CERT_RCAS = [CPD, ODLM, PODS, AUTOC, WXO]  # the cert incident cluster (+MCOG doc)

Q = []
def add(id, q, intents, must, facts, nice=None, abstain=False):
    Q.append(dict(id=id, question=q, intents=intents, must_have=must,
                  nice_to_have=nice or [], key_facts=facts, must_abstain=abstain))

# ── Part 1 — Business (all corpus-wide → thematic) ────────────────────────────
add("B1", "How many distinct production incidents do we have on record, and how are they distributed across the months from October 2025 to January 2026?",
    ["thematic"], [],
    ["There are 11 RCA documents covering about 10 distinct incidents",
     "October 2025 is the heaviest month",
     "Incidents span October 2025 through January 2026"])
add("B2", "Are certificate-related incidents getting more or less frequent over the period, and what does that say about our certificate process?",
    ["thematic"], CERT_RCAS,
    ["Certificate incidents are clustered and recurring, not improving",
     "Around five certificate incidents occurred in roughly ten weeks",
     "Certificate lifecycle management is the dominant systemic risk"])
add("B3", "Which incidents happened during the peak-season change freeze or backup windows, and is that timing a pattern?",
    ["thematic"], [CPD, DIGIT, E0050],
    ["The CPD Nov 10 change was deferred by an environment freeze",
     "The Jan 8 spike and Jan 15-17 0050E occurred during Central offline backups",
     "Freeze/backup timing is a recurring risk pattern"])
add("B4", "What is the single most common root cause across all incidents, and how many separate outages trace back to it?",
    ["thematic"], CERT_RCAS,
    ["Certificates are the most common root cause",
     "About five distinct certificate incidents (six certificate-related RCA documents)",
     "No other cause is close"])
add("B5", "Which incidents share the same underlying failure mode even though they looked different on the surface?",
    ["thematic", "similar"], [PODS, ODLM, AUTOC],
    ["Certificate renewal/re-issuance triggering pod restarts is the shared failure mode",
     "Oct 2 (cert-manager), Oct 20 (ODLM), and Dec 19-20 (auto renewal) share it"])
add("B6", "What's our most dangerous unresolved risk going into peak?",
    ["thematic", "causal"], [AUTOC, MCOG],
    ["The MCOG bottleneck is the top unresolved risk",
     "It is explicitly called out as still unresolved in the Dec 19-20 RCA",
     "It made pod-restart recovery take about a day"])
add("B7", "Which teams are engaged most often across incidents, and which incidents needed the most cross-team coordination?",
    ["thematic"], [],
    ["The IBM EM (Essential Management) team is the constant responder",
     "Frequent partners include UPS Voice, Red Hat, and the ODLM Support team"])
add("B8", "When a certificate incident happens, who typically gets involved in resolving it?",
    ["thematic", "similar"], [CPD, ODLM],
    ["IBM EM team leads certificate incident resolution",
     "Red Hat and the ODLM Support team handle operator/cert-manager bugs",
     "UPS authentication services and UPS Voice coordinate trust-store/VGW issues"])
add("B9", "How often did an incident require a war room or escalation to a Duty Manager, and did escalation work?",
    ["thematic"], [AUTOC, MCOG, VGW, E0050],
    ["War rooms/bridges were used in Dec 19-20, MCOG, and the Oct 14 VGW incident",
     "Escalation failed in the CWSMR0050E incident: the hotline call was missed",
     "The Duty Manager was the sole responder due to unplanned leave"])
add("B10", "Which incidents had the largest customer impact, by calls affected and outage duration?",
    ["thematic"], [VGW, ODLM, AUTOC, CPD],
    ["Voice Gateway Oct 14 forwarded ~5,000 calls during a ~30-minute outage",
     "ODLM Oct 20 transferred 422 calls (East) and 244 (Central)",
     "Dec 19-20 had a 30-minute full outage plus ~a day of degradation"])
add("B11", "How much of our customer impact came from Voice versus Digital?",
    ["thematic"], [],
    ["Customer impact was predominantly Voice",
     "Digital-impacting incidents were the Dec response-time spikes and the Jan 8 spike"])
add("B12", "What was our typical resolution time, and which incidents resolved fast vs slow?",
    ["thematic"], [CPD, VGW, WEBHK, PODS, ODLM, AUTOC],
    ["Misconfiguration/human-error incidents resolved in minutes (CPD, VGW, Log Webhook)",
     "Bug/capacity incidents were slow (ODLM multi-day, Dec 19-20 ~a day)"])
add("B13", "How often was a communication or coordination gap a contributing factor, and where?",
    ["thematic"], [CPD, AUTOC, E0050],
    ["The CPD Nov 10 RCA lists communication gaps between the VGW and delivery teams",
     "The CWSMR0050E incident was largely a communication/escalation failure"])
add("B14", "Which preventive measures appear across multiple incidents — i.e. promises we keep making, suggesting they aren't being implemented?",
    ["thematic"], [CPD, ODLM, AUTOC],
    ["Proactive certificate monitoring/alerting ahead of renewal recurs across CPD, ODLM, and Dec 19-20",
     "It appears at least three times, showing it wasn't fully operationalized"])
add("B15", "Did any of our own changes (CRs) cause incidents, and were the right remediation changes made afterward?",
    ["thematic"], [CPD, RESP],
    ["The CPD route certificate update caused the Nov 10 voice outage",
     "The Dec 1 digital load-balancer replacement caused the response-time spikes",
     "Remediation CRs were made (e.g. Patch ODLM, Rollout Restart cert-manager)"])
add("B16", "What kinds of changes do we make most often, and what does that say about where engineering effort goes?",
    ["thematic"], [],
    ["Voice/Voice-Gateway/speech-adapter configuration is the largest change category (~99)",
     "Capacity/scaling is the second largest (~66)",
     "Engineering effort concentrates on Voice enablement and capacity tuning"])
add("B17", "If you had to brief leadership in three sentences on our reliability over this period, what would you say?",
    ["thematic"], [],
    ["About 10 production incidents over Oct 2025-Jan 2026, majority Voice-affecting",
     "The dominant theme is certificate renewals triggering pod restarts",
     "The top open risk is the unresolved MCOG bottleneck"])
add("B18", "Which incidents are still open or have unresolved follow-ups?",
    ["thematic"], [E0050, MCOG, DIGIT, RESP],
    ["CWSMR0050E has no permanent fix and the deployed fix is undocumented",
     "The MCOG bottleneck is explicitly unresolved",
     "The Jan 8 digital incident was left for UPS to follow up"])

# ── Part 2 — Technical Engineer (single-incident) ─────────────────────────────
add("T1", "Explain the exact mechanism by which the ODLM operator bug caused pod restarts.",
    ["causal"], [ODLM],
    ["ODLM's certificate cache went stale",
     "It wrongly re-issued certificates even when not near expiry",
     "Re-issuing a certificate to a pod forces the pod to restart"])
add("T2", "Why did a missing Kafka permission cause an hour of latency instead of an outright failure?",
    ["causal"], [WEBHK],
    ["The Message_logs topic had only 1 partition instead of 3",
     "wa-kafka-user lacked the Alter permission to create partitions",
     "The system silently degraded, funneling all traffic through one pod"])
add("T3", "Trace the full dependency chain from the Nov 10 CPD route certificate update to dropped voice calls.",
    ["causal"], [CPD],
    ["The Voice Gateway keeps CPD certificates in a local trust store",
     "The renewed certs were not imported into the VGW trust store",
     "The VGW could not validate the new certs, so voice calls dropped"], nice=[CR_CPD])
add("T4", "How does the Voice Gateway trust store relate to the CPD route certificates, and why did updating one without the other cause the outage?",
    ["causal"], [CPD],
    ["The VGW validates TLS to CPD using certs in its trust store",
     "Updating route certs without updating the trust store breaks trust",
     "The two must be changed together"], nice=[CR_CPD])
add("T5", "What exact ACL was added to fix the Log Webhook issue, and what was the desired vs actual partition state?",
    ["resolution", "causal"], [WEBHK],
    ["The Alter operation permission on topic resources was added to wa-kafka-user",
     "Desired state was 3 partitions, actual was 1"])
add("T6", "Which CRs implemented the Log Webhook fix, and what did each do?",
    ["resolution"], [WEBHK],
    ["One CR granted the Kafka partition-increase privilege and rolling-restarted webhooks",
     "Another increased the CPU limit on wa-incoming-webhooks to clear the backlog"], nice=[CR_KAFKA, CR_WEBCPU])
add("T7", "What were the steps in the CR that refreshed the CPD certificates, and which step triggered the incident?",
    ["causal", "resolution"], [CR_CPD],
    ["Steps included recreating cpd-tls-secret, updating the VGW trust store, and updating the CPD routes",
     "Updating the CPD routes before the trust store was synchronized triggered the dropped calls"], nice=[CPD])
add("T8", "How was the Oct 2 pod-restart incident actually fixed, and by which CR?",
    ["resolution"], [PODS],
    ["cert-manager could not find cached secrets and looped certificate re-issuance",
     "The fix was a rollout restart of cert-manager to refresh its cache"], nice=[CR_CERTMGR])
add("T9", "How was the ODLM issue remediated technically?",
    ["resolution"], [ODLM],
    ["A hotfix CS 4.11 image was obtained from IBM Support",
     "The image was mirrored to the GCP artifactory and the ODLM CSV was patched to use it"], nice=[CR_ODLM])
add("T10", "What configuration change addressed the MCOG bottleneck?",
    ["resolution"], [MCOG],
    ["CPU requests/limits were added to the noobaa ocs-storagecluster resources",
     "max_connections was set to 2400 on noobaa"], nice=[CR_MCOG])
add("T11", "Given MCOG is still unresolved, what's the blast radius if speech-cr certs auto-renew again before it's fixed?",
    ["causal", "thematic"], [AUTOC, MCOG],
    ["An auto-renewal would restart STT and TTS runtime pods",
     "The MCOG bottleneck would prevent pods from reaching Ready, slowing recovery",
     "Impact would be high for Voice across both clusters"])
add("T12", "We're on CPD 5.1.2 / CS 4.11. What's the risk the ODLM bug recurs, and what blocks the permanent fix?",
    ["causal"], [ODLM],
    ["The permanent fix is CS 4.13, which is incompatible with CPD 5.1.2",
     "We run a hotfixed 4.11, so recurrence is possible until CPD is upgraded"])
add("T13", "cert-manager 1.17.0 is flagged unsupported and size-limited — which incident did it contribute to and what's the exposure?",
    ["causal", "similar"], [WXO],
    ["It contributed to the WxO certificate failure",
     "The cert request exceeded size limits due to 96+ DNS entries from UAB",
     "Disabling UAB was the workaround"])
add("T14", "What's the recommended recovery procedure when STT/TTS pods get stuck in a restart loop after a cert renewal?",
    ["resolution"], [AUTOC],
    ["Scale the affected deployments down to about 10 replicas",
     "Then incrementally scale up in controlled batches while diverting traffic"])
add("T15", "What diagnostic procedure applies if the diagnostics job itself starts causing pod restarts?",
    ["resolution"], [MCOG],
    ["If the diagnostics job is still running, locate and delete the zen-watchdog-serviceability-job",
     "Including chuck container logs during collection triggered STT/TTS restarts"], nice=[AUTOC])
add("T16", "The ODLM RCA references the MCOG RCA — how are the two incidents technically connected?",
    ["similar", "causal"], [ODLM, MCOG],
    ["Both involve pods restarting and being slow to recover",
     "ODLM causes restarts; the MCOG bottleneck makes restarts recover slowly"])
add("T17", "What was the precise trigger and timestamp of the MCOG bottleneck incident?",
    ["causal", "timeline"], [MCOG],
    ["It was triggered by the speech-cr-certificate being renewed on 2025-12-19",
     "The IBM Software Hub diagnostics script/job was a contributing factor"])
add("T18", "What caused the Voice Gateway-to-Assistant outage on Oct 14, and was it an authorized change?",
    ["causal"], [VGW],
    ["An engineer inadvertently disabled the private DNS zone while testing Apigee connectivity",
     "It was not a properly authorized PROD change",
     "About 5,000 calls were transferred during the ~30-minute outage"])

# ── Part 3 — Hardest (depth x breadth) ────────────────────────────────────────
add("H1", "For Dec 19-20: what triggered it, why did recovery take ~a day, what manual procedure finally worked, and has this failure mode happened before and since?",
    ["causal", "thematic"], [AUTOC],
    ["Automatic certificate renewal restarted STT/TTS/WA pods",
     "The unresolved MCOG bottleneck kept pods from reaching Ready, slowing recovery",
     "Scale down to ~10 replicas then incremental scale-up recovered it"], nice=[MCOG, PODS, ODLM])
add("H2", "List every certificate-related incident in chronological order, and for each give what broke, who resolved it, and the preventive promise — then say which promise repeats.",
    ["thematic"], CERT_RCAS,
    ["The certificate incidents are Oct 2, Oct 20 (ODLM), Oct 22-24 (WxO), Nov 10 (CPD), and Dec 19-20",
     "Proactive certificate monitoring ahead of renewal is the repeated promise"])
add("H3", "The Nov 10 CPD outage and the Dec 19-20 outage were both certificate-driven but different. Compare failure, blast radius, and fix.",
    ["similar", "thematic"], [CPD, AUTOC],
    ["Nov 10 was a human/process failure fixed by reverting the route change",
     "Dec 19-20 was an automated failure with MCOG-slowed recovery, fixed by scale down/up",
     "Nov 10 hit Voice only ~30 min; Dec 19-20 hit both clusters and channels for ~a day"])
add("H4", "New on-call engineer: what's the most dangerous recurring failure pattern, which incidents prove it, the current mitigation, and what's still unresolved?",
    ["thematic"], [PODS, ODLM, AUTOC],
    ["The recurring pattern is certificate renewal/re-issuance triggering pod restarts",
     "Proven by Oct 2, Oct 20, and Dec 19-20",
     "The MCOG bottleneck remains unresolved"], nice=[MCOG])
add("H5", "Across incidents during offline backups or change freezes, what's the common operational risk, and how did it show up in Voice vs Digital?",
    ["thematic"], [DIGIT, E0050, CPD],
    ["The common risk is load concentration / deferred-change exposure",
     "Digital: Jan 8 spike and Dec response-time spikes; Voice/both: CPD freeze and 0050E"], nice=[RESP])
add("H6", "Map our changes to our incidents: which CRs caused incidents, which responded to incidents, and is there a change that did both?",
    ["thematic"], [CPD, RESP],
    ["The CPD-routes cert update and the Dec 1 load-balancer replacement caused incidents",
     "Patch ODLM, cert-manager restart, Kafka/CPU, MCOG specs, and LB CRs responded to incidents",
     "Certificate-renewal activity both responds to expiring certs and caused the Dec 19-20 outage"])
add("H7", "How did our handling of certificate incidents evolve from October to December — did we get better at it?",
    ["thematic"], [PODS, ODLM, CPD, AUTOC],
    ["Early responses (Oct) were reactive: restart cert-manager, hotfix ODLM",
     "By Dec there was a cert-rotation playbook, 30-day alerting, and pre-peak triggers",
     "Process matured faster than the underlying capacity risk (MCOG still open)"])
add("H8", "Which incidents involved a single point of failure (human, process, or component), and what was the SPOF in each?",
    ["thematic"], [VGW, E0050, WEBHK, CPD],
    ["VGW Oct 14: human SPOF (one engineer disabled the private DNS zone)",
     "CWSMR0050E: process SPOF (one Duty Manager missed the hotline)",
     "Log Webhook: component SPOF (single Kafka partition via a missing ACL)"])
add("H9", "If we fixed exactly one underlying thing, which would prevent the most future incidents, and what's the evidence?",
    ["thematic", "causal"], [PODS, ODLM, CPD, AUTOC],
    ["Making certificate renewals non-disruptive would prevent the most incidents",
     "It would address Oct 2, Oct 20, Nov 10, and Dec 19-20 — the largest cluster"], nice=[MCOG])
add("H10", "Reconstruct the complete Dec 19-20 timeline across BOTH clusters in one ordered narrative.",
    ["timeline"], [AUTOC],
    ["Dec 19 2:05 PM PROD-East speech-cr certs renewed, triggering STT/TTS restarts",
     "Dec 19 6:00 PM PROD-Central certs renewed causing a 30-minute outage",
     "Full recovery by Dec 20 night via scale down to 10 then incremental scale-up"], nice=[MCOG])
add("H11", "Across all incidents, which named IBM Support / ServiceNow / Salesforce cases were opened, and to what do they map?",
    ["thematic"], [ODLM, MCOG, PODS, VGW, WXO, AUTOC],
    ["ODLM: ServiceNow TS020583334 and Red Hat 04271299",
     "MCOG: TS021050847 and TS021052405; Pods Oct 2: TS020438303",
     "VGW: TS020527054/TS020527078; WxO: TS020579868"])
add("H12", "Which incidents were caused by automation/known bugs vs human error vs capacity, and what's the split?",
    ["thematic"], [],
    ["Automation/known bug dominates (ODLM, cert-manager, Dec 19-20, MCOG, WxO)",
     "Human error is rare (only the Oct 14 VGW DNS incident)",
     "Capacity/config covers Log Webhook and the response-time/backup incidents"])
add("H13", "Compare the two response-time spike incidents (Dec 1-3 vs Jan 8) — same root cause or different?",
    ["similar", "thematic"], [RESP, DIGIT],
    ["They are different root causes",
     "Dec 1-3 was a load-balancer replacement with session/Redis churn",
     "Jan 8 was a Central offline backup pushing all traffic to East with UPS API failures"])
add("H14", "What single sentence captures the systemic story of this engagement, backed by the incident record?",
    ["thematic"], [],
    ["A Voice-heavy watsonx deployment whose dominant failure mode is certificate renewals triggering pod restarts",
     "Amplified by an unresolved MCOG bottleneck and backup/freeze load concentration"])

# ── Negative / abstention traps (test honesty about gaps) ─────────────────────
add("N1", "What caused the database outage in the Frankfurt region in March 2026?",
    ["causal"], [], [], abstain=True)
add("N2", "How much did the November 10 CPD certificate outage cost UPS in dollars?",
    ["causal", "thematic"], [CPD], [], abstain=True)
add("N3", "What exact permanent fix did UPS deploy for the CWSMR0050E error on January 17?",
    ["resolution"], [E0050],
    ["The exact fix is not documented and there is no permanent fix yet"], abstain=True)


def main():
    out = Path(__file__).with_name("eval_gold.jsonl")
    with out.open("w", encoding="utf-8") as f:
        for q in Q:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    from collections import Counter
    cats = Counter(i for q in Q for i in q["intents"])
    print(f"Wrote {len(Q)} questions -> {out}")
    print("Acceptable-intent tags:", dict(cats))
    print("Abstention traps:", sum(1 for q in Q if q['must_abstain']))


if __name__ == "__main__":
    main()
