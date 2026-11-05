# Real Hermes CLI and Desktop validation — 0.3.0

Recorded **November 5, 2026, 20:14–20:27 UTC** against disposable **CapRover 1.16.0**, using CapRover Operator **0.3.0**, the native Hermes CLI, a live **gpt-6.1-sol** model and **Hermes Desktop attached to the same isolated session**. The run upgraded the official CyberChef image from **11.4.0 to 11.5.0**, retained uncertainty through a real process restart, refused a repeated apply and reconciled to dated runtime/HTTP evidence. The plugin deployment POST count ended at **one**.

The [public annotated trace](../evidence/real-hermes-cli-trace-v4.json) has SHA-256 `7868ad0057e08f5ebb15e0ec87fd07b96ba0a75bbe87111eb459e1005f48710f`. The original raw trace remains unchanged with SHA-256 `01ac85209f066854811af523ea66ba16084d9327346b322546ff2a2cf1e93290`.

## Native UI and capture attribution

**User-authorized lab automation operated the actual native approval flow.** This was not a manual human keypress, and no synthetic approval callback was injected. Both CLI and Desktop were connected; the exact UI that submitted the approval response was not attributed. Accordingly, this evidence does not claim CLI-only approval or a human click.

The raw recorder used the fixed label `unmodified native CLI` for `scope.approval_surface`. The public copy corrects that descriptive field to reflect the connected CLI and Desktop and adds `capture_annotations` documenting the raw checksum, correction, restart and visual observations. It also replaces the private workspace prefix in ten presentation `file`/`preview` fields with `/lab/hermes-home`, preserving every suffix and content-hash filename. **Operation results, states, checks, process events, POST counters, backend acknowledgements, source hashes and app manifest are unchanged.** The annotation is not a replacement for those observations.

The generated operation card was observed inline in the real Hermes Desktop conversation through computer-use inspection. Its file was produced by the plugin from the same tool result and displayed through Hermes's public `::preview` surface. The card's uncertain state showed no runtime or HTTP proof and recommended reconciliation. The later exported card showed the verified receipt and its observation time. The submitted capture set contains the native Desktop context and original app screens; their filenames and SHA-256 hashes are recorded in `capture_annotations`.

The exact generated HTML snapshots are also preserved in this repository: [planned](../evidence/cards-v4/0957feec1ad5446685ee4fe206e90f36304ff9c16ef4e40e127a6edf5bfa415c.html), [uncertain](../evidence/cards-v4/6e66a6c006b066b37f32c47e95d517174c6fd90427426343d06c0d4aa2e4a447.html) and [verified](../evidence/cards-v4/12621585c33b09084d59a9768174fc1b2828085f8e3572e60041cfeee3eedd07.html). Each filename matches the SHA-256 of its unchanged bytes. These static files can be opened locally; they are historical receipts, not live controls.

## Observed sequence

| Step | Recorded observation | Plugin deployment POST attempts |
| --- | --- | --- |
| 1 | `caprover_doctor` returned `ready_for_planning`; `caprover_plan` saved the requested immutable CyberChef image against baseline app version 1 | 0 |
| 2 | Native approval; the proxy received real HTTP 200 / Captain status 100 from CapRover, then deliberately dropped that acknowledgement to the client | 1 |
| 3 | `caprover_apply` returned `reconcile_required`; its exported card remained visibly uncertain | 1 |
| 4 | The first CLI process exited. The isolated Desktop/gateway were stopped; gateway PID 96824 was replaced by PID 184. A second CLI process used the preserved profile/ledger | 1 |
| 5 | `caprover_status` read the same `reconcile_required` operation; another `caprover_apply` was refused with `plan_not_applicable` | 1 |
| 6 | Explicit `caprover_reconcile` returned `verified`: newer exact-image metadata, one current running task, configured/running replicas 1/1 and HTTP 200 | 1 |
| 7 | Final local status retained the same dated verified operation | 1 |

The verified observation is dated **2026-11-05T20:26:09Z**. The two CLI process events in the trace are separate evidence from the gateway restart; `capture_annotations.gateway_restart` records the latter's process identities and source checksum. The lab proxy and CapRover remained available during the Hermes restart so the request counter continued across both CLI processes.

This run went directly from uncertainty to verified task/HTTP evidence when reconciliation was requested. The earlier dispatcher run's intermediate metadata-only state is separate evidence and is not attributed to this run.

## Real open-source application outcome

[CyberChef](https://github.com/gchq/CyberChef) is licensed under [Apache-2.0](https://github.com/gchq/CyberChef/blob/v11.5.0/LICENSE). The official image serves its browser application without a server database in this scenario. The example gives a small team a useful new capability in software it hosts and controls; the plugin handles the bounded deployment and recovery from the lost response.

- Baseline 11.4.0: `ghcr.io/gchq/cyberchef@sha256:379c6cbcfd8cc10b8e045548f3ebaa82ce429e4b40cf60e4532e116a4c67884d`.
- Requested 11.5.0: `ghcr.io/gchq/cyberchef@sha256:f04a39926344faf8e4cbbc8c92fb125d2b466f9be62db09aff23c7c406101a51`.

CyberChef's [11.5.0 changelog](https://github.com/gchq/CyberChef/blob/v11.5.0/CHANGELOG.md) introduces **Parse PGP Key**. The original 11.4.0 UI showed its version and lacked that operation. After deployment, the original 11.5.0 UI showed the new operation and parsed the public RFC 9580 fixture from the project's [official test](https://github.com/gchq/CyberChef/blob/v11.5.0/tests/operations/tests/PGP.mjs), `Parse PGP Key: RFC9580 legacy V4 (A.1)`. Its real output included fingerprint `C959BDBAFA32A2F89A153B678CFDE12197965A9A`, algorithm EdDSA and curve ed25519Legacy. No private key or user credential was used.

The after capture used `localhost:62744` to avoid stale browser content under `127.0.0.1:62744`; both addressed the same fixed loopback lab port. The version modal and functional output were checked after loading. These browser observations are distinct from the plugin's CapRover task-image and HTTP checks. The app itself was not patched or replaced with a mock interface.

## Source identity and preserved history

All **12 of 12** `plugin_source_sha256` entries matched the repository bytes for **0.3.0** before adding this documentation. They cover the native adapter, package code including `presentation.py`, manifest and visual-fixture script. The adapter, engine and HTTP client were the default implementation; the deliberate lost acknowledgement was injected by the isolated proxy.

The CLI banner reported an unknown version. Seven files from the live managed CLI/gateway runtime matched public Hermes commit [`f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`](https://github.com/NousResearch/hermes-agent/commit/f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab), tagged `v0.22.6`: `run_agent.py`, `model_tools.py`, `agent/tool_executor.py`, `hermes_cli/plugins.py`, `hermes_cli/middleware.py`, `tools/approval.py` and `tools/approval_context.py`. The hashes are included in the trace annotations. This identifies the compared files without claiming that every runtime file was checked. The separately installed Desktop UI bundle reported **0.22.7**, build `b200f5b`; it is a distinct component from that compared runtime.

The earlier [0.2.0 CLI validation](real-cli-validation.md) and [trace](../evidence/real-hermes-cli-trace.json) remain separate and unchanged. That trace still has SHA-256 `ff368de18d50230a21a87adfb83bb95dc279ad0573df85275f02375c1537dc82`. The earlier [dispatcher validation](real-lab-validation.md) used a simulated callback and remains explicitly labeled as such.

The public v4 trace retains structured tool results, historical card facts and local artifact paths. It contains no raw credentials, authorization headers, private keys, app environment definitions or raw session logs. Local URLs and paths identify the disposable capture environment; they are not public demonstration endpoints.

## Lab boundary and limits

The authorized Docker-in-Docker laboratory used an independent daemon and Swarm with fixed loopback host ports. App registration, configuration and the initial 11.4.0 deployment were setup writes outside the plugin's one-POST count. Temporary TLS trust was scoped to the isolated processes; no production service was targeted. After capture, the lab container and its private volumes, the dedicated network and the owned demo/gateway processes were removed or stopped. Capture files and the exact HTML snapshots were preserved. Cleanup evidence is summarized in the trace annotations; the local lab endpoints are no longer running.

This is one integration run on CapRover 1.16.0. `verified` is a dated observation, not continuous availability. Cards cannot grant approval, execute, retry, refresh evidence or roll back; their attempt marker does not establish a server-request count. This static-app update does not prove database migration safety, preservation of persistent app data, multi-node behavior, exclusive causation, distributed exactly-once delivery or production certification.
