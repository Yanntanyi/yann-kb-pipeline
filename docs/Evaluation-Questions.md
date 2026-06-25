# Traversal Test Questions — Two User Lenses (with answer keys)

*Owner of the business perspective: **Yann**. This is our test battery for how well
the system actually answers real questions — not toy lookups, but questions that
force it to combine many documents, reason over time, and connect people, changes,
and incidents. **Every question includes a verified correct answer** drawn from the
13 RCAs and the change-request corpus, so when we run the question in the chat UI we
can compare the system's response against ground truth.*

> How to use: ask the question in the chat UI, then check the response against the
> **Correct answer**. Score it on (a) did it retrieve the right incidents/changes,
> (b) did it state the key facts, (c) did it avoid inventing anything. The
> `Tests:` note says what traversal capability the question is probing.

---

## Who the business user is (and what they're really after)

The business user is **not a lighter-weight engineer** — they are a fundamentally
different reader of the same data. Think delivery managers, account leads,
service-delivery executives, IBM EM leadership, and the UPS stakeholders they
report to. They rarely open a single incident to debug it. Instead they treat the
whole incident-and-change history as a **management information system** and ask
what the *pattern across everything* says about the health of the engagement.

**Their core concerns:**
- **Service health as a trend.** Are we getting better or worse? Is a class of
  problem growing? They think in months and quarters, not minutes.
- **Risk concentration.** Where is our fragility? What single thing, if it broke,
  would hurt us most? What keeps almost-hurting us?
- **Accountability and follow-through.** We wrote down preventive actions after
  each incident — are we actually doing them, or making the same promise again?
- **People and process.** Who gets pulled into incidents? Which teams carry the
  load? Where do hand-offs and communication break down? Did escalation work?
- **Customer-facing impact.** How many calls dropped, how long were we down, which
  channel (Voice vs Digital) bore the pain, and how fast did we recover?

**Their mindset is retrospective and strategic**, where the engineer's is
immediate and diagnostic. A good answer *for the business user* is **aggregated,
quantified, comparative, and names the players** — "across the period, 6 of our 11
incidents were certificate-related, concentrated Oct–Dec, mostly Voice, and the
same 'monitor certs ahead of renewal' preventive action appears in 4 of them,
which means it isn't being operationalized." That is a different shape of answer
than "pod X restarted because cert Y was reissued."

**Why this matters for the project:** business questions are overwhelmingly
**aggregation + temporal + people-pivot** questions — read *wide* across the
corpus. That is precisely the shape our current document-to-document traversal is
weakest at (it walks a handful of docs from a few seeds and caps at ~6). So the
business lens is also our sharpest test of whether the architecture can scale from
"explain one incident" to "report across all of them." Owning this perspective
means owning that requirement.

---

## Part 1 — Business User questions (18)

### 1a. Trends & volume over time

**B1 — How many distinct production incidents do we have on record, and how are
they distributed across the months from October 2025 to January 2026?**
- **Correct answer:** 13 RCAs exist; ~11 are distinct production incidents (two
  RCAs — *MCOG bottleneck* and *Automatic Certificate Renewal* — describe the same
  Dec 19–20 event from different angles). Distribution: **Oct 2025** is the
  heaviest (Pods Restarted Oct 2, Voice Gateway/DNS Oct 14, ODLM Oct 20, WxO cert
  Oct 22–24); **Nov** (CPD voice outage Nov 10); **Dec** (Response-time spikes
  Dec 1–3, Dec 19–20 cert-renewal restarts); **Jan 2026** (Digital response-time
  spike Jan 8, CWSMR0050E Jan 15–17). Log Webhook latency is early Oct (10/8).
- *Tests:* corpus-wide count + temporal bucketing. *Source:* all RCAs.

**B2 — Are certificate-related incidents getting more or less frequent over the
period, and what does that say about our certificate process?**
- **Correct answer:** They are **clustered and recurring, not improving**:
  cert-manager auto-renewal restarts (Oct 2), ODLM cert re-issue (Oct 20), WxO cert
  size (Oct 22–24), CPD trust-store outage (Nov 10), and the Dec 19–20 auto-renewal
  restarts — five+ certificate incidents in ~10 weeks. The trend says our
  certificate **lifecycle management is the dominant systemic risk** and that
  renewals repeatedly trigger restarts; it's the reason for remediation CRs like
  *Change Certificate Renewal Dates to 1 year* and the *TLS Cert Renewal* manual
  pre-peak trigger.
- *Tests:* temporal trend over one theme + inference. *Source:* CPD, ODLM, Pods Oct 2, Auto-Cert, WxO RCAs.

**B3 — Which incidents happened during the peak-season change freeze or backup
windows, and is that timing a pattern?**
- **Correct answer:** Yes, a clear pattern. The **CPD Nov 10 outage** traces to a
  change *deferred by the environment freeze* then implemented partially. The
  **Jan 8 Digital spike** and **Jan 15–17 CWSMR0050E** both occurred during
  **Central-cluster offline backups** that pushed all traffic to East. The Dec
  19–20 RCA also notes the team *agreed not to modify MCOG during the peak freeze*.
  Timing-driven risk (freezes/backups concentrating load or deferring fixes) is a
  recurring theme.
- *Tests:* condition-based linking across incidents. *Source:* CPD, Digital Jan 8, 0050E, Auto-Cert RCAs.

### 1b. Recurring causes & risk concentration

**B4 — What is the single most common root cause across all incidents, and how
many separate outages trace back to it?**
- **Correct answer:** **Certificates.** At least **6** incidents: CPD trust-store
  (Nov 10), ODLM cert re-issue (Oct 20), cert-manager auto-renewal (Oct 2),
  automatic cert renewal (Dec 19–20), MCOG bottleneck (triggered by a speech-cr
  certificate renewal Dec 19), and WxO certificate-size failure (Oct). No other
  cause comes close.
- *Tests:* aggregation/counting across corpus — the headline business question. *Source:* 6 RCAs.

**B5 — Which incidents share the same underlying failure mode even though they
looked different on the surface?**
- **Correct answer:** The **"certificate renewal/re-issuance triggers pod
  restarts"** family: Oct 2 (cert-manager auto-renewal), Oct 20 (ODLM re-issue),
  and Dec 19–20 (automatic renewal of speech-cr/internal-TLS certs). All three are
  the same mechanism — a cert event forces pods to restart — despite different
  operators and triggers. MCOG (Dec 19) is adjacent: a cert renewal exposed a
  separate bottleneck that *slowed* the restarts.
- *Tests:* clustering by mechanism, not keywords. *Source:* Pods Oct 2, ODLM, Auto-Cert, MCOG RCAs.

**B6 — What's our most dangerous *unresolved* risk going into peak?**
- **Correct answer:** The **MCOG (Multi-Cloud Object Gateway) bottleneck.** It is
  explicitly called out as **still unresolved** in the Dec 19–20 RCA and was the
  reason pod restarts took ~a full day to recover; the team agreed not to modify it
  during the freeze. Combined with the ongoing cert-renewal-restart pattern, an
  auto-renewal before MCOG is fixed is the standout exposure.
- *Tests:* current-state synthesis + forward risk. *Source:* Auto-Cert, MCOG RCAs.

### 1c. People & teams (the "players")

**B7 — Which teams are engaged most often across incidents, and which incidents
needed the most cross-team coordination?**
- **Correct answer:** The **IBM EM (Essential Management) team** is the constant
  responder across nearly every incident. Frequent partners: **UPS Voice team**,
  **UPS authentication services**, **Red Hat** (ODLM/cert-manager, WxO
  cert-manager), the **ODLM Support team**, **IBM Product Development** (0050E),
  **UPS-Search team** (Voice Gateway DNS incident), and the **NOC/Duty Manager**
  function. Highest cross-team coordination: the **CPD Nov 10** outage (IBM EM, UPS
  Voice GW, UPS auth services) and **Dec 19–20** (EM, offshore EM, Voice SMEs, UPS).
- *Tests:* entity/people pivot + aggregation. *Source:* all RCAs.

**B8 — When a certificate incident happens, who typically gets involved in
resolving it?**
- **Correct answer:** IBM EM team leads; **Red Hat** and the **ODLM Support team**
  for operator/cert-manager bugs; **UPS authentication services** and **UPS Voice**
  for trust-store/Voice Gateway coordination (CPD); named individuals include
  **Daryl** (CPD coordination). Resolution usually runs through IBM Support cases
  (e.g. ODLM TS020583334, RedHat 04271299).
- *Tests:* entity pivot scoped to a theme. *Source:* CPD, ODLM RCAs.

**B9 — How often did an incident require a war room or escalation to a Duty
Manager, and did escalation work?**
- **Correct answer:** War rooms/bridges were initiated in **Dec 19–20** (warroom),
  **MCOG** (war room, traffic diverted), and the **Voice Gateway Oct 14** incident
  (MS Teams bridge). Escalation **failed** in the **CWSMR0050E Jan 15–17** incident:
  UPS called the EM hotline during peak and the call was **missed** because the Duty
  Manager was the sole primary/secondary responder (unplanned leave) — prompting a
  process fix (auto-raise Sev1 after 3–4 unanswered rings). Net: escalation usually
  works but the hotline single-point-of-failure is a documented gap.
- *Tests:* process aggregation + an exception. *Source:* Auto-Cert, MCOG, VGW, 0050E RCAs.

### 1d. Impact, duration & channel reporting

**B10 — Which incidents had the largest customer impact, by calls affected and
outage duration?**
- **Correct answer:** Largest: **Voice Gateway Oct 14** — ~**5,000 calls** forwarded
  to call-center agents during a ~30-minute outage (all in-progress and new calls
  transferred). **ODLM Oct 20** — 422 calls transferred (Prod East) + 244 (Prod
  Central). **Dec 19–20** — a 30-minute full outage in Prod-Central plus ~a day of
  degraded service. **CPD Nov 10** — ~30-minute voice interruption. Smaller: MCOG
  (~10 min Prod East), Digital Jan 8 (~10 min).
- *Tests:* impact/duration aggregation + ranking. *Source:* VGW, ODLM, Auto-Cert, CPD, MCOG, Digital RCAs.

**B11 — How much of our customer impact came from Voice versus Digital?**
- **Correct answer:** **Predominantly Voice.** Voice-impacting: CPD (Nov 10), ODLM
  (Oct 20), Pods Oct 2, MCOG, Voice Gateway (Oct 14), and the Voice side of Dec
  19–20. Digital-impacting: Response-time spikes (Dec 1–3), Digital response-time
  spike (Jan 8), and the Digital/WA side of Dec 19–20 (the 30-min Prod-Central
  outage hit both). CWSMR0050E (Jan) hit UPS APIs broadly. WxO is Orchestrate
  (non-customer-facing non-prod). So Voice carries the majority of customer-facing
  outage minutes.
- *Tests:* channel-segmented aggregation. *Source:* all RCAs.

**B12 — What was our typical resolution time, and which incidents resolved fast vs
slow?**
- **Correct answer:** **Fast (minutes):** CPD Nov 10 (reverted within minutes),
  Voice Gateway Oct 14 (root-caused and fixed in ~22 min, 11:17→11:39), Log Webhook
  (stabilized 10–15 min after the permission fix). **Slow (hours–days):** Pods Oct 2
  (~4.5 hrs, 2:52→7:25 PM), ODLM (multi-day: Oct 20 incident → Oct 24 hotfix → Oct
  28 patched), Dec 19–20 (~a full day to full recovery). Pattern: misconfig/human-
  error incidents resolved fast; **bug/capacity incidents (ODLM, MCOG) were slow**.
- *Tests:* MTTR aggregation + segmentation. *Source:* CPD, VGW, Log Webhook, Pods Oct 2, ODLM, Auto-Cert RCAs.

### 1e. Process, communication & follow-through

**B13 — How often was a communication or coordination gap a contributing factor,
and where?**
- **Correct answer:** Repeatedly. The **CPD Nov 10** RCA lists "gaps in
  communication between the Voice Gateway implementation team and the delivery team"
  and "misalignment" as contributing factors. **Dec 19–20** lessons cite log-
  collection controls and stakeholder communication. **CWSMR0050E** is largely a
  communication/escalation failure (missed hotline, unclear fix details). Comms/
  coordination is one of the most repeated non-technical contributing factors.
- *Tests:* thematic aggregation over contributing-factor sections. *Source:* CPD, Auto-Cert, 0050E RCAs.

**B14 — Which preventive measures appear across *multiple* incidents — i.e.
promises we keep making, suggesting they aren't being implemented?**
- **Correct answer:** Two stand out. (1) **Proactive certificate monitoring /
  alerting ahead of renewal** appears in CPD ("enhanced monitoring and early
  alerts"), ODLM ("certificates need to be monitored and a plan in advance of
  renewal"), and Dec 19–20 ("alert stakeholders 30 days prior to expiration"). It is
  partly operationalized via CRs (*Change Certificate Renewal Dates to 1 year*, the
  *TLS Cert Renewal* pre-peak manual trigger) — but its repetition shows it wasn't
  fully in place. (2) **"Initiate a bridge call / war room early and test in
  Non-Prod before Prod"** recurs in the Oct 2 and Voice Gateway RCAs. This is the
  accountability finding: the same cert-monitoring promise is made 3+ times.
- *Tests:* meta-analysis across preventive-measures sections — the killer business question. *Source:* CPD, ODLM, Auto-Cert RCAs + cert CRs.

**B15 — Did any of our own changes (CRs) cause incidents, and were the right
remediation changes made afterward?**
- **Correct answer:** Yes on both. **Changes that caused incidents:** the **CPD
  route certificate update** (Nov 10) directly caused the voice outage; the **Dec 1
  Digital load-balancer replacement** (cpd.ccca.ups.com → va-chat.ups.com, new
  cookie-based stickiness) caused the Dec 1–3 response-time spikes. **Remediation
  changes made:** *Patch ODLM in Prod/Non-Prod* (for ODLM), *Rollout Restart
  cert-manager in Prod* (for the Oct 2 restarts), *Add user privilege to increase
  Kafka partition* + *Increase CPU limits for wa-incoming-webhooks* (for Log
  Webhook), *Set MCOGs CPU Specs* (for MCOG), and the *Configure new Digital load
  balancer to use cookies* / *New Load Balancer* CRs (for the LB spikes).
- *Tests:* CR↔RCA causal mapping. *Source:* CPD, Response-time, ODLM, Pods Oct 2, Log Webhook, MCOG RCAs + named CRs.

**B16 — What kinds of changes do we make most often, and what does that say about
where engineering effort goes?**
- **Correct answer:** Of ~281 CRs, the dominant categories are **Voice/Voice-Gateway
  tenant & speech-adapter configuration** (tenant onboarding, STT/TTS model updates,
  SIP/media-relay tuning — the single largest group), **capacity/scaling** (memory,
  CPU, PVC storage, replicas, HPA), **custom-crawler image updates** (many version
  bumps), **load-balancer/DNS/networking**, and **certificates**. Effort is heavily
  concentrated in **Voice enablement and capacity tuning**, with certificates a
  smaller but disproportionately incident-causing slice. *(Counts are approximate —
  derived from CR titles, not a full audit.)*
- *Tests:* corpus-wide categorization of changes. *Source:* CR corpus (titles).

**B17 — If I had to brief leadership in three sentences on our reliability over
this period, what would I say?**
- **Correct answer:** "Over Oct 2025–Jan 2026 we handled ~11 production incidents,
  the majority Voice-affecting and the single largest theme being certificate
  renewals/re-issuance triggering pod restarts (6 incidents). Customer impact peaked
  with the Oct 14 Voice Gateway outage (~5,000 calls) and the Dec 19–20 cert-renewal
  event (~a day of degraded Voice/Digital). Our top open risk is the unresolved MCOG
  bottleneck, and our recurring miss is operationalizing proactive certificate
  monitoring, which we've now committed to three times."
- *Tests:* full-corpus executive synthesis (the business deliverable itself). *Source:* all.

**B18 — Which incidents are still open or have unresolved follow-ups?**
- **Correct answer:** **CWSMR0050E (Jan 15–17)** — the exact fix UPS deployed is
  undocumented and there is no permanent fix (Monday.com ticket open with Shantunu;
  Product Dev engaged). **MCOG bottleneck** — explicitly unresolved. **Digital
  response-time (Jan 8)** — left as "UPS to follow up with their service team."
  **Response-time spikes (Dec)** — ends in next-steps/diagnostics, not a closed root
  cause. Cleanly closed: CPD, ODLM, Log Webhook, Voice Gateway, WxO.
- *Tests:* status aggregation + abstention on "unknown" cases. *Source:* 0050E, MCOG, Digital, Response-time RCAs.

---

## Part 2 — Technical Engineer questions (18)

The engineer uses the system to **understand mechanics and predict what breaks**.
Answers should be precise, name components/configs, and trace dependencies.

**T1 — Explain the exact mechanism by which the ODLM operator bug caused pod
restarts.**
- **Correct answer:** ODLM (Operand Deployment Lifecycle Manager) issues
  certificates. A bug in the running version made its **certificate cache go
  stale**, so it wrongly concluded certificates needed re-issuing even when not near
  expiry. Each time it re-issues a cert to a pod, that pod **restarts** to load the
  new cert — producing random rolling restarts. *Source:* ODLM RCA + Patch ODLM CR.

**T2 — Why did a missing Kafka permission cause an hour of latency instead of an
outright failure?**
- **Correct answer:** The `Message_logs` topic was configured for 3 partitions but
  only **1** existed, because `wa-kafka-user` **lacked the `Alter` operation
  permission on topic resources**, so the extra partitions could never be created.
  The system **silently degraded** — all webhook traffic funneled through a single
  `wa-incoming-webhook` pod, creating ~1 hour of backlog rather than erroring out.
  *Source:* Log Webhook RCA.

**T3 — Trace the full dependency chain from the Nov 10 CPD route certificate update
to dropped voice calls.**
- **Correct answer:** CPD route certs were updated → the **Voice Gateway keeps CPD
  certs in a local trust store** → the renewed certs were **not yet imported into
  the VGW trust store** → VGW could not establish trusted connections for calls →
  **voice calls dropped** (2:25–2:55 PM EST) → changes reverted to restore service.
  The CR sequence (recreate `cpd-tls-secret` → update trust-store-file-secret →
  patch VGW CR → update routes) shows the trust-store step was the dependency that
  was skipped/incomplete due to the freeze. *Source:* CPD RCA + CPD-routes CR.

**T4 — How does the Voice Gateway trust store relate to the CPD route certificates,
and why did updating one without the other cause the outage?**
- **Correct answer:** The VGW validates TLS to CPD using certs in its **trust
  store**. The CR explicitly pairs a CPD route cert refresh with a **trust-store
  update** (new `trust-store-file-secret-<date>` + VGW CR patch). Updating the route
  certs while the trust store still held the old chain meant the VGW didn't trust the
  new certs → connection failures. They must change together. *Source:* CPD-routes CR + CPD RCA.

**T5 — What exact ACL was added to fix the Log Webhook issue, and what was the
desired vs actual partition state?**
- **Correct answer:** Added to `wa-kafka-user`: `operation: Alter`, `resource type:
  topic`, `name: '*'`, `patternType: literal`, `host: '*'`. Desired state = **3
  partitions** for `Message_logs` (per the wa-store deployment CR); actual = **1**.
  After the ACL, 3 partitions were created and load spread across 3 pods. *Source:* Log Webhook RCA.

**T6 — Which CRs implemented the Log Webhook fix, and what did each do?**
- **Correct answer:** (1) *Add user privilege to increase Kafka partition and
  rolling restart incoming webhooks* — granted the partition-increase privilege and
  rolling-restarted `wa-incoming-webhooks` then `wa-store`. (2) *Increase CPU limits
  for wa-incoming-webhooks* — raised the CPU limit to **2** to clear the Kafka
  `Message_log` backlog faster (processing dropped from ~2500 ms to ~550 ms avg),
  expected to return to ~0.1 after. *Source:* the two named CRs.

**T7 — What were the steps in the CR that refreshed the CPD certificates, and which
step triggered the incident?**
- **Correct answer:** Steps: (1) validate new cert chain (tls.crt/tls.key/ca.crt),
  (2) delete & recreate `cpd-tls-secret`, (3) update the Voice Gateway trust store +
  patch the VGW CR to reference the new secret + restart VGW pods, (4) **update CPD
  routes** to the new cert, (5) post-validation/cleanup. The **route update (step 4)
  performed before the trust store was synchronized** is what triggered the dropped
  calls. *Source:* CPD-routes CR + CPD RCA.

**T8 — How was the Oct 2 pod-restart incident actually fixed, and by which CR?**
- **Correct answer:** Root cause was **cert-manager unable to find cached secrets,
  looping certificate re-issuance** and restarting pods. Fix CR: *Rollout Restart
  cert-manager in Prod* — `oc rollout restart deployment/cert-manager -n
  cert-manager` (on Red Hat's advice) to refresh the cache and re-evaluate
  certificates, run in off-peak hours. *Source:* Pods Oct 2 RCA + cert-manager CR.

**T9 — How was the ODLM issue remediated technically?**
- **Correct answer:** A **hotfix CS 4.11 image** (`odlm:4.3.12-66282c`) was obtained
  from IBM Support, **mirrored to the GCP artifactory** with Skopeo, then the **ODLM
  operator's ClusterServiceVersion (CSV) was patched** to use the new image;
  reconcile was monitored. Needed because CPD 5.1.2 only supports CS 4.11 and the
  proper fix (CS 4.13) was incompatible. *Source:* ODLM RCA + Patch ODLM CR.

**T10 — What configuration change addressed the MCOG bottleneck?**
- **Correct answer:** *Set MCOGs CPU Specs…* CR: add **CPU requests** to
  `ocs-storagecluster` resources `noobaa-core: 4`, `noobaa-db: 4`,
  `noobaa-endpoint: 4`; add a **CPU limit** `noobaa-endpoint: 4`; and add `dbConf`
  setting **`max_connections: 2400`** on `noobaa`. Rationale: HPA on the noobaa
  endpoint requires requests/limits, and the noobaa endpoint was a suspected MCOG
  bottleneck point. *Source:* MCOG CR (+ MCOG RCA).

**T11 — Given MCOG is still unresolved, what's the blast radius if speech-cr certs
auto-renew again before it's fixed?**
- **Correct answer:** High for Voice. In Dec 19–20, an automatic **speech-cr/
  internal-TLS cert renewal** restarted STT (79 replicas/cluster) and TTS (46
  replicas/cluster) runtime pods, and the **MCOG bottleneck prevented pods from
  reaching Ready**, stretching recovery to ~a day with a 30-min full outage. Same
  preconditions → likely repeat of cascading STT/TTS restarts with slow recovery
  across both clusters. (Forward inference — system should reason from the two RCAs
  and flag uncertainty.) *Source:* Auto-Cert + MCOG RCAs.

**T12 — We're on CPD 5.1.2 / CS 4.11. What's the risk the ODLM bug recurs, and
what blocks the permanent fix?**
- **Correct answer:** The permanent fix is **CS 4.13**, which is **incompatible with
  CPD 5.1.2**, so we run a **hotfixed 4.11**. Risk: until CPD is upgraded to a
  version supporting CS 4.13, recurrence is possible if the hotfix regresses or a
  cluster isn't patched (the patch had to be applied to all clusters). *Source:* ODLM RCA.

**T13 — cert-manager 1.17.0 is flagged unsupported and size-limited — which incident
did it contribute to and what's the exposure?**
- **Correct answer:** The **WxO (Watson Orchestrate) certificate failure**: the cert
  request exceeded size limits because the legacy **UAB** component added **96+ DNS
  entries**, and **Red Hat cert-manager v1.17.0** couldn't handle the request size
  (and is no longer supported by CP4D Product Management). Workaround was disabling
  UAB (`uab.enabled: false`). Exposure: any dynamic service that inflates DNS
  entries on this cert-manager version risks the same; switching to **wildcard DNS
  entries** is the recommended structural fix. *Source:* WxO RCA.

**T14 — What's the recommended recovery procedure when STT/TTS pods get stuck in a
restart loop after a cert renewal?**
- **Correct answer:** **Scale down then incrementally scale up.** In Dec 19–20:
  divert traffic to the healthy cluster, scale the affected deployments down to ~**10
  replicas**, then scale up in controlled batches (STT +4–5, TTS +2–5 per
  iteration), monitoring to full count (79 STT / 46 TTS), then restore traffic. This
  achieved 100% recovery in both clusters. *Source:* Auto-Cert RCA.

**T15 — What diagnostic procedure applies if the diagnostics job itself starts
causing pod restarts?**
- **Correct answer:** In the MCOG incident the **IBM Software Hub diagnostics
  script/job** was a contributing factor; the RCA says if the diagnostics job is
  still running, **locate the `zen-watchdog-serviceability-job` and delete it**. In
  Dec 19–20, including the **`chuck` container logs** during collection triggered
  simultaneous STT/TTS restarts; the fix is the 5.3.0 change to collect custom logs
  in the chuck pod (which has sufficient ephemeral storage) instead. *Source:* MCOG + Auto-Cert RCAs.

**T16 — The ODLM RCA references the MCOG RCA — how are the two incidents technically
connected?**
- **Correct answer:** Both involve **pods restarting and being slow to come back**.
  ODLM's RCA explicitly cross-references the MCOG "stt/tts runtime pods stall/
  terminate" RCA (and ticket TS020521674) as a *related slow-pod-restart* issue:
  ODLM is the *cause* of restarts in one case; MCOG is the *bottleneck that makes
  restarts recover slowly* in the other. They compound when a cert event triggers
  restarts while MCOG is bottlenecked. *Source:* ODLM RCA (cross-ref) + MCOG RCA.

**T17 — What was the precise trigger and timestamp of the MCOG bottleneck incident?**
- **Correct answer:** The MCOG bottleneck (a known issue) was triggered by the
  **`speech-cr-certificate` being renewed on 2025-12-19T23:06:13 UTC**, with the IBM
  Software Hub diagnostics script/job as a contributing factor; ~half the
  `speech-cr-stt-runtime` pods became unhealthy and traffic was diverted to Prod
  Central. *Source:* MCOG RCA.

**T18 — What caused the Voice Gateway-to-Assistant outage on Oct 14, and was it an
authorized change?**
- **Correct answer:** An IBM Delivery Engineer, while testing **Apigee
  connectivity** for an upcoming RAG release from the cluster bastion,
  **inadvertently disabled the private DNS zone**, breaking VGW↔Watson Assistant
  connectivity; ~5,000 calls were transferred over ~30 min. **Not a properly
  authorized/announced PROD change** — the preventive measures are to notify EM/UPS
  and **get CR approval before PROD connectivity tests** and to test in Non-Prod.
  Fix: re-enable the private DNS zone. *Source:* Voice Gateway RCA.

---

## Part 3 — The "hardest" questions (14): depth × breadth fused

These bundle multiple intents, multiple documents, and time. A flat top-K retriever
should visibly fail them; even our traversal is tested on gathering *everything*.

**H1 — For Dec 19–20: what triggered it, why did recovery take ~a day, what manual
procedure finally worked, and has this failure mode happened before and since?**
- **Correct answer:** Trigger — automatic renewal of **speech-cr/internal-TLS
  certificates** in both clusters restarted STT/TTS (and WA) pods; including `chuck`
  logs forced a simultaneous restart. Slow recovery — the **unresolved MCOG
  bottleneck** kept pods from reaching Ready. Procedure that worked — **scale down to
  ~10 replicas then incremental scale-up** with traffic diversion. Before — Oct 2
  (cert-manager auto-renewal) and Oct 20 (ODLM re-issue) are the same family.
  *Source:* Auto-Cert + MCOG + Pods Oct 2 + ODLM RCAs.

**H2 — List every certificate-related incident in chronological order, and for each
give what broke, who resolved it, and the preventive promise — then say which
promise repeats.**
- **Correct answer:** **Oct 2** Pods Restarted — cert-manager auto-renewal restarted
  speech pods; IBM EM + Red Hat; promise: rolling-restart safeguards / scheduling.
  **Oct 20** ODLM — operator re-issued certs, pod restarts; IBM EM + ODLM Support;
  promise: stay current on versions, **monitor certs ahead of renewal**. **Oct
  22–24** WxO — oversized cert request (UAB/cert-manager 1.17.0); IBM EM + Red Hat;
  promise: wildcard DNS, audit cert-manager support. **Nov 10** CPD — trust store
  missing renewed certs; IBM EM + UPS auth/Voice; promise: cert dependency checklist
  + **early monitoring/alerts**. **Dec 19–20** Auto-Cert/MCOG — auto renewal restarts
  + MCOG bottleneck; IBM EM (+offshore); promise: cert-rotation playbook, **alert 30
  days prior**, fix MCOG. **Repeats:** *proactive certificate monitoring/alerting
  ahead of renewal* appears in ODLM, CPD, and Dec 19–20 — the un-operationalized
  promise. *Source:* Pods Oct 2, ODLM, WxO, CPD, Auto-Cert RCAs.

**H3 — The Nov 10 CPD outage and the Dec 19–20 outage were both certificate-driven
but different. Compare failure, blast radius, and fix.**
- **Correct answer:** **Nov 10 CPD** — *human/process* failure: a manual route-cert
  update outran the **trust-store sync** (worsened by the freeze); blast radius ~30
  min Voice only; fix = **revert** the route change (minutes). **Dec 19–20** —
  *automated* failure: scheduled cert auto-renewal restarted STT/TTS/WA pods, and
  **MCOG** slowed recovery; blast radius = both clusters, Voice **and** Digital, 30-
  min full outage + ~a day degraded; fix = **scale-down/up recovery**, not a revert.
  Same theme (certs), opposite nature (manual-coordination vs automated-renewal).
  *Source:* CPD + Auto-Cert RCAs.

**H4 — New on-call engineer: what's the most dangerous recurring failure pattern,
which incidents prove it, the current mitigation, and what's still unresolved?**
- **Correct answer:** Pattern — **certificate renewal/re-issuance triggers pod
  restarts**, occasionally cascading. Proof — Oct 2, Oct 20 (ODLM), Dec 19–20.
  Current mitigations — ODLM hotfix image; cert-manager rollout-restart;
  *Change Certificate Renewal Dates to 1 year* + auto-renew 1 month early outside
  peak; manual pre-peak TLS renewal; scale-down/up recovery runbook. **Unresolved**
  — the **MCOG bottleneck** (slows recovery) and full operationalization of
  proactive cert monitoring. *Source:* Pods Oct 2, ODLM, Auto-Cert, MCOG RCAs + cert CRs.

**H5 — Across incidents during offline backups or change freezes, what's the common
operational risk, and how did it show up in Voice vs Digital?**
- **Correct answer:** Common risk — **load concentration / deferred-change exposure**
  when one cluster is out for backup or changes are frozen. **Digital:** Jan 8 spike
  (Central in offline backup → East served all traffic → API failures/latency) and
  the Dec response-time spikes (traffic moved to Central). **Voice/both:** the
  CWSMR0050E Jan 15–17 errors during a Central backup (AKAMAI blocked APIs under
  load), and CPD Nov 10 where the **freeze deferred** the cert change into a partial,
  inconsistent state. *Source:* Digital Jan 8, Response-time, 0050E, CPD RCAs.

**H6 — Map our changes to our incidents: which CRs caused incidents, which responded
to incidents, and is there a change that did both?**
- **Correct answer:** **Caused:** CPD-routes cert update → CPD voice outage; Dec 1
  Digital load-balancer replacement → response-time spikes. **Responded:** Patch
  ODLM (ODLM), Rollout Restart cert-manager (Oct 2), Kafka-privilege + wa-incoming-
  webhooks CPU (Log Webhook), Set MCOGs CPU Specs (MCOG), New Load Balancer /
  cookie-stickiness (response-time spikes). **Both:** the **certificate-renewal
  activity** — a planned *response* to expiring certs that itself *caused* the Dec
  19–20 outage; similarly the **load-balancer change** was a planned improvement that
  caused spikes and then required follow-up LB CRs. *Source:* CPD, Response-time,
  ODLM, Pods Oct 2, Log Webhook, MCOG RCAs + named CRs.

**H7 — How did our handling of certificate incidents evolve from October to
December — did we get better at it?**
- **Correct answer:** Evolution is visible. Early (Oct 2, Oct 20) responses were
  **reactive** — restart cert-manager, hotfix ODLM after the fact. By Nov 10 the RCA
  introduces a **cert-dependency checklist and pre-change validation**. By Dec 19–20
  the team has a **cert-rotation playbook, 30-day-prior alerting, 1-year renewal CRs,
  and pre-peak manual triggers** — clearly more proactive. But recovery still
  depended on manual scaling and MCOG stayed unresolved, so process matured faster
  than the underlying capacity risk. *Source:* Pods Oct 2, ODLM, CPD, Auto-Cert RCAs + cert CRs.

**H8 — Which incidents involved a single point of failure (human, process, or
component), and what was the SPOF in each?**
- **Correct answer:** **Voice Gateway Oct 14** — *human* SPOF (one engineer's
  bastion test disabled the private DNS zone). **CWSMR0050E Jan 15–17** — *process*
  SPOF (one Duty Manager as both primary and secondary responder, missed the
  hotline). **Log Webhook** — *component/permission* SPOF (single Kafka partition via
  a missing ACL → one overloaded pod). **CPD Nov 10** — *process* SPOF (trust-store
  step dependent on coordination that didn't happen). *Source:* VGW, 0050E, Log Webhook, CPD RCAs.

**H9 — If we fixed exactly one underlying thing, which would prevent the most future
incidents, and what's the evidence?**
- **Correct answer:** **Make certificate renewals non-disruptive** (renew without
  forcing pod restarts / sequence trust-store updates / fix the MCOG bottleneck that
  slows restart recovery). Evidence: it would directly address Oct 2, Oct 20, Nov 10,
  and Dec 19–20 — the largest and most repeated cluster — versus point fixes that
  each address one incident. *Source:* Pods Oct 2, ODLM, CPD, Auto-Cert, MCOG RCAs.

**H10 — Reconstruct the complete Dec 19–20 timeline across BOTH clusters in one
ordered narrative.**
- **Correct answer:** Dec 19 **2:05 PM** PROD-East speech-cr certs renewed → STT/TTS
  rolling restart; **2:15 PM** chuck-log collection (SEV-2 TS021052405) → all STT/TTS
  restart; **2:30 PM** warroom; **2:35 PM** traffic → PROD-Central; **2:35–11:35 PM**
  incremental scale recovery; **11:35 PM** East recovered (79/79 STT, 46/46 TTS).
  Dec 19 **6:00 PM** PROD-Central internal-TLS + speech-cr certs renewed → WA/STT/TTS
  restart → **30-min outage** (HTTP 500); 6:30 PM Digital recovered. Dec 20 **5:50
  AM** traffic diverted Central→East (more restarts); **7:30 AM** scale to 10 then
  up; **12:00 PM** partial (32/79 STT, 25/46 TTS); **11:55 PM** full recovery.
  *Source:* Auto-Cert RCA (+ MCOG for the Dec 19 STT detail).

**H11 — Across all incidents, which named IBM Support / ServiceNow / Salesforce
cases were opened, and to what do they map?**
- **Correct answer:** ODLM — ServiceNow **TS020583334**, Red Hat **04271299** (Sev1).
  MCOG — **TS021050847, TS021052405**. Pods Oct 2 — **TS020438303** (Sev2→Sev1).
  Voice Gateway — **TS020527054, TS020527078**. WxO — **TS020579868**. Auto-Cert
  (Dec 19–20) — Salesforce **TS021052405**. ODLM also references **TS020521674**
  (MCOG-related). *Source:* respective RCAs.

**H12 — Which incidents were caused by automation/known bugs vs human error vs
capacity, and what's the split?**
- **Correct answer:** **Automation/known bug:** ODLM (operator bug), cert-manager
  Oct 2 (cache bug), Dec 19–20 (auto-renewal w/o readiness validation), MCOG (known
  bottleneck), WxO (cert-manager size limit). **Human error:** Voice Gateway Oct 14
  (DNS zone disabled). **Capacity/config:** Log Webhook (Kafka partition/permission),
  Response-time spikes (LB/session + Redis load), Digital Jan 8 & 0050E (backup load
  + AKAMAI). Split: **automation/bug dominates** (~5), then capacity (~3–4), human
  error rare (1). *Source:* all RCAs.

**H13 — Compare the two "response-time spike" incidents (Dec 1–3 vs Jan 8) — same
root cause or different?**
- **Correct answer:** **Different.** Dec 1–3 — a **load-balancer replacement** (new
  cookie-based session stickiness, all traffic to Central, Redis session churn, 404s
  from the Google GLB); fixed by splitting traffic 50/50. Jan 8 — a **Central offline
  backup** pushed all traffic to East, with **UPS-owned APIs (Tracking, Account
  Lookup) high-latency and Config API 403s**; left for UPS to follow up. Shared
  symptom (latency spikes), different causes (our LB change vs backup-load + UPS API
  behavior). *Source:* Response-time + Digital Jan 8 RCAs.

**H14 — What single sentence captures the systemic story of this engagement, backed
by the incident record?**
- **Correct answer:** "A Voice-heavy watsonx deployment whose dominant, recurring
  failure mode is **certificate renewals/re-issuance triggering pod restarts** —
  amplified by an unresolved **MCOG bottleneck** and load concentration during
  backups/freezes — where process maturity (cert playbooks, monitoring commitments)
  has outpaced the still-open underlying capacity and automation fixes." *Source:* synthesis of all RCAs + cert CRs.

---

## Part 4 — Owning the business perspective (leadership stance)

**The thesis.** The business user reads the corpus *wide* — trends, recurring
causes, people, impact, follow-through — where the engineer reads it *deep*. Those
two readings stress traversal in opposite ways, and a complete system must serve
both.

**The finding I lead with.** Business questions are overwhelmingly **aggregation +
temporal + people-pivot** (B1–B18 above almost all require reading across many
incidents at once). That is exactly what our current document-to-document traversal
is weakest at: it walks ~6 docs from a few seeds, so it cannot reliably *count
across the corpus*, *roll up by month*, or *pivot on a person/team* that links
separate incidents. **So the business perspective is the forcing function that
proves whether we need a corpus-wide aggregation/reporting capability** (entity and
time as first-class), not just deep traversal — which ties straight to the
entity-graph discussion.

**How I run it with the team.**
- I own the **definition of the business user** (the section above) and the
  **business question battery** (Part 1 + breadth-heavy H2/H4/H5/H7) as the
  acceptance test for that persona.
- I report results as a verdict per question *shape*: can the system answer
  read-wide questions yet, or only read-deep ones? The answer keys make this
  objective — we compare the chat response to ground truth.
- That hands the architecture owner a concrete, user-grounded requirement
  ("the business persona needs corpus-wide aggregation + temporal + people pivots")
  instead of a preference. The technical battery (Part 2) pairs with whoever owns
  the architecture/technical lens; H1/H3/H6/H10 are shared deep-traversal tests we
  all contribute to.

**Standup one-liner:** *"I own the business user. Their questions read wide —
trends, recurring causes, players, follow-through — which is exactly what our
depth-first traversal can't do yet. So the business lens is also our sharpest test
of whether the architecture can scale from 'explain one incident' to 'report on all
of them' — and I've written 18 of those with answer keys so we can measure it."*
