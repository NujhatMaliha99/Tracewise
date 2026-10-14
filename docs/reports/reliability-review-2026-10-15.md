# Incident Review: c965320330d1

DRAFT / operator review required

Generated: 2026-10-14T19:03:44.086+00:00
Environment: development
Status: investigating / Owner: Demo platform team
Observed interval: 2026-10-14T18:53:59.096478Z to 2026-10-14T18:54:03.201497Z

## Evidence Summary

3 requests / 6 retained records / 6 error records.
Services: otel-gateway
Candidate dependencies (unverified): otel-tasks

## Operator Notes

- 2026-10-14T19:02:12.170+00:00 / Local operator: Controlled demo: three failed gateway requests after deliberately stopping the temporary tasks listener. This establishes a local transport failure, not a verified production root cause.

## Nearby Changes (Temporal Context Only)

- 2026-10-14T18:55:00+00:00 / otel-gateway / configuration: Controlled demo configuration marker / revision local-sdk-demo

## Follow-Up Actions

| Action | Owner | Deadline UTC | Status |
| --- | --- | --- | --- |
| Verify recovery and review dependency timeout policy | Demo platform team | 2026-10-16T12:00:00+00:00 | in\_progress |

## Timeline (Latest Retained Events)

- 2026-10-14T18:53:59.113+00:00 / Local operator / opened: {"candidate\_dependencies": \["otel-tasks"\], "trace": "d33e8f0de3fe3c94cdf916aeae8fb420"}
- 2026-10-14T18:53:59.113+00:00 / Local operator / evidence\_added: {"records": 1, "trace": "d33e8f0de3fe3c94cdf916aeae8fb420"}
- 2026-10-14T18:53:59.129+00:00 / Local operator / evidence\_added: {"records": 1, "trace": "d33e8f0de3fe3c94cdf916aeae8fb420"}
- 2026-10-14T18:54:01.161+00:00 / Local operator / evidence\_added: {"records": 1, "trace": "d836c1029d2f363aa6e3cb0eacca09b4"}
- 2026-10-14T18:54:01.172+00:00 / Local operator / evidence\_added: {"records": 1, "trace": "d836c1029d2f363aa6e3cb0eacca09b4"}
- 2026-10-14T18:54:03.196+00:00 / Local operator / evidence\_added: {"records": 1, "trace": "a21da438dcb08ed99ad5ef121a8be3ab"}
- 2026-10-14T18:54:03.229+00:00 / Local operator / evidence\_added: {"records": 1, "trace": "a21da438dcb08ed99ad5ef121a8be3ab"}
- 2026-10-14T18:56:29.842+00:00 / Local operator / followup\_created: {"action\_id": "3989e650a871", "title": "Verify recovery and review dependency timeout policy"}
- 2026-10-14T18:56:41.428+00:00 / Local operator / followup\_updated: {"action\_id": "3989e650a871", "status": "in\_progress", "owner": "Demo platform team"}
- 2026-10-14T19:02:12.170+00:00 / Local operator / operator\_update: {"status": "investigating", "owner": "Demo platform team", "note": "Controlled demo: three failed gateway requests after deliberately stopping the temporary tasks listener. This establishes a local transport failure, not a verified production root cause."}

## Evidence Index

| Evidence ID | Request | Service | Event UTC | HTTP / Level |
| --- | --- | --- | --- | --- |
| 6453c5369b83497caa484ae4ee4b7244 | d33e8f0de3fe3c94cdf916aeae8fb420 | otel-gateway | 2026-10-14T18:53:59.096478Z | None / ERROR |
| f7931780238f426eb8d627aa7f14735a | d33e8f0de3fe3c94cdf916aeae8fb420 | otel-gateway | 2026-10-14T18:53:59.117397Z | 503 / ERROR |
| e151127638924a559850ebbfdef09f6c | d836c1029d2f363aa6e3cb0eacca09b4 | otel-gateway | 2026-10-14T18:54:01.143412Z | None / ERROR |
| 90da5c5b4c064e20882ce612a8ba5f26 | d836c1029d2f363aa6e3cb0eacca09b4 | otel-gateway | 2026-10-14T18:54:01.165799Z | 503 / ERROR |
| 279bcd8dd75f4460856029560942a440 | a21da438dcb08ed99ad5ef121a8be3ab | otel-gateway | 2026-10-14T18:54:03.178844Z | None / ERROR |
| af620fb945564792b074531c68155e6d | a21da438dcb08ed99ad5ef121a8be3ab | otel-gateway | 2026-10-14T18:54:03.201497Z | 503 / ERROR |

## Boundaries

- Draft assembled from retained evidence; operator review required.
- Observed failure interval is not confirmed user-impact duration or MTTR.
- Dependency candidates and nearby changes are not verified causes.
- Timeline is capped at 200 events per related incident; changes capped at 50; evidence retention is bounded.
