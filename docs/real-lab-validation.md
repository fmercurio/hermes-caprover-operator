# Real disposable CapRover validation

Recorded **November 5, 2026, 17:29:28 UTC** with CapRover **1.16.0**, native Hermes PluginManager/dispatcher, and this plugin's default HTTP client and engine. [Sanitized execution trace](../evidence/real-caprover-native-trace.json), SHA-256 `513748b1399ada8d2f8b71ddb6cd434380c65a68faed4556f1dd0a1fd51976a8`.

The lab used an explicitly authorized, privileged Docker-in-Docker container with an independent daemon, its own single-node Swarm and no production credentials or host Docker socket mounted into it. Only fixed loopback ports were contacted. The lab was prepared separately: app creation, configuration and the initial image deployment are setup operations, **not included in the plugin's one-POST count**.

**Human approval was simulated by an explicitly labeled native host callback.** No person clicked an approval during this run. The actual native approval hooks, middleware, execution permit, ledger and tool dispatcher ran in fresh Hermes processes. No core monkeypatch or HTTP-client replacement was used.

## Observed sequence

| Event | Real observation | Plugin deployment POST attempts |
| --- | --- | --- |
| 1. Plan | Saved app `operator-demo`, baseline version 1 and the existing immutable image | 0 |
| 2. Apply, lost ACK | Native simulated host approval; real backend deployment request; durable `reconcile_required` | 1 |
| 3. Controlled connection loss | The proxy received real HTTP 200 / Captain status 100 before closing the incoming connection without its acknowledgement | 1 |
| 4. New-process replay refusal | Another Hermes process reopened the ledger; the same apply was refused without another approval or deployment request | 1 |
| 5. Metadata and HTTP observed | Version 2 recorded the requested image and HTTP returned 200, but old/new tasks overlapped in the same slot; `runtime_rollout_proven` remained false and status was `metadata_http_observed` | 1 |
| 6. Runtime convergence | A later explicit read observed one current slot running the requested immutable image, desired/running count 1, and HTTP 200; status became `verified` | 1 |

This run directly observed why metadata plus HTTP 200 must not be treated as a completed image rollout. Event 5 contained the new running task and an old running task whose desired state was shutdown. Verification waited for the contradictory overlap to disappear.

Images used:

- Initial: `nginx@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236`
- Requested: `nginx@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de`

## Transport and reproducibility

[demo/real_caprover_lab.py](../demo/real_caprover_lab.py) targets only the already-created lab's fixed ports, app and images. It creates an ephemeral HTTPS loopback proxy and lab CA; private files are owner-only and removed afterward. Only child Hermes processes receive `SSL_CERT_FILE` for that CA. TLS verification stays enabled; no global trust store, keychain or production client setting is changed. The proxy routes fixed API paths to the lab and a fixed health path to its app. It forwards a deployment request once, waits for the backend response and deliberately drops that response.

The script requires the separately prepared isolated lab, the exact initial image state, OpenSSL and an interpreter with Hermes installed. It does not provision or stop Docker, create apps, reset a successful deployment, or accept arbitrary server/image/credential options. It creates its output file exclusively and cannot overwrite an earlier trace. Re-running against the already-updated app requires separate lab reset; it must not be used against an existing production daemon.

## Limits

This is one real integration run on one disposable CapRover version. It is not production certification, a real human-approval test, multi-node coverage, migration validation, ongoing monitoring, distributed exactly-once delivery or proof that no external actor could cause an observed deployment. `verified` records the dated CapRover task-API and HTTP evidence. The trace contains public results, simulated approval labels and counters; it excludes credentials, raw environment definitions, private keys and raw server logs.

The synthetic test suites separately cover drift, expiry, malformed schemas, authorization provenance and concurrent/crashed processes. Those cases were not all repeated against this real server.
