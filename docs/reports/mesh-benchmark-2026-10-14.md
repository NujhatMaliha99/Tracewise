# Tracewise Multi-Service Fault Report

Experiment: 307e6e8b9d08 / complete
Created UTC: 2026-10-14T16:33:54.195+00:00

Controlled local multi-service experiments / actual HTTP and SQLite faults.
Three seeds, four scenarios, 12 runs, 144 actual gateway probe checks and 972
recorded client/server spans. Nine correlated candidate episodes were produced.
No missed injected incidents or duplicate detector alerts were observed in the
nine fault runs; no false alerts were observed in the three baseline runs.
These are measured outcomes for this small controlled matrix, not accuracy claims.

12 sequential manual probe checks per run; threshold 2/2, no scheduler,
isolated from user monitors and SMTP.

| Seed | Scenario | Detection s | Recovery s | Missed | Duplicate | False alerts | P95 header ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 7 | baseline | N/A | N/A | N/A | N/A | 0 | 58.76 |
| 7 | payments-timeout | 0.4053 | 0.0807 | 0 | 0 | N/A | 189.59 |
| 7 | service-down | 0.3505 | 0.1315 | 0 | 0 | N/A | 191.27 |
| 7 | database-contention | 0.3295 | 0.0996 | 0 | 0 | N/A | 190.12 |
| 19 | baseline | N/A | N/A | N/A | N/A | 0 | 74.01 |
| 19 | payments-timeout | 0.3401 | 0.0693 | 0 | 0 | N/A | 188.16 |
| 19 | service-down | 0.3845 | 0.0644 | 0 | 0 | N/A | 180.94 |
| 19 | database-contention | 0.3469 | 0.098 | 0 | 0 | N/A | 191.69 |
| 42 | baseline | N/A | N/A | N/A | N/A | 0 | 58.48 |
| 42 | payments-timeout | 0.3969 | 0.1123 | 0 | 0 | N/A | 207.29 |
| 42 | service-down | 0.3564 | 0.0958 | 0 | 0 | N/A | 187.77 |
| 42 | database-contention | 0.3769 | 0.097 | 0 | 0 | N/A | 191.59 |

## Boundaries

Seeds repeat fault placement and request spacing, not OS/network timing.
Detection seconds are for this accelerated protocol, not production polling
intervals. Faults last four checks and were deliberately chosen to cross the
two-failure threshold. This is not a stress/load test or independent real-incident
evaluation. Baseline absence of false alerts in 36 checks is not a false-positive
rate guarantee. No email is sent by the experiment suite.

- Latencies measure actual gateway response headers, including propagation.
- Detection begins after fault activation; recovery begins after fault removal.
- P95 uses the floor-index empirical sample quantile over 12 observations.
- Baseline missed/duplicate metrics and fault-run false-alert metrics are N/A.
- Correlation is a hypothesis, not a verified root cause. Inspect spans and IDs.
- Client/server spans overlap; their durations must not be summed as wall time.
- A timed-out payments server can finish successfully after its caller fails.
- No production detection guarantee, real dataset or autonomous repair is claimed.

Reproduce the protocol from Fault experiments using seeds 7, 19, 42 and all four
scenarios. Timings and UUID request IDs will differ. Full span evidence is retained
in the local experiment workspace and available through the report JSON export;
runtime databases are excluded from Git.
