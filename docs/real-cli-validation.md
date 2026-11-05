# Real Hermes CLI validation

Recorded **November 5, 2026, 19:06–19:14 UTC** against disposable **CapRover 1.16.0**, using the native Hermes CLI and live **gpt-6.1-sol** model. The [sanitized trace](../evidence/real-hermes-cli-trace.json) has SHA-256 `ff368de18d50230a21a87adfb83bb95dc279ad0573df85275f02375c1537dc82`.

Two fresh CLI processes shared the private plugin ledger. The run used the default plugin adapter, engine and HTTP client. A lab proxy received real HTTP 200 / Captain status 100 for the deployment, then deliberately lost the acknowledgement on the client connection. The plugin's deployment POST counter ended at **one**.

## Native approval and model scope

The real model made the native tool calls. **User-authorized lab automation selected the response in the actual, unmodified CLI approval UI.** This was an automated UI action in an explicitly authorized disposable lab, not a manual human keypress. No synthetic host approval callback was injected into this run. The earlier [dispatcher validation](real-lab-validation.md) and automated test callbacks are separate evidence.

## Observed sequence

| Step | Native tool and recorded result | Plugin deployment POST attempts |
| --- | --- | --- |
| 1 | `caprover_doctor` returned `ready_for_planning`, with explicit limits on local storage and native approval checks | 0 |
| 2 | `caprover_plan` saved one immutable image against app `operator-demo`, baseline version 1 | 0 |
| 3 | Native UI approval; `caprover_apply` returned `reconcile_required` after the lost acknowledgement | 1 |
| 4 | A second CLI process read the same operation with `caprover_status`; uncertainty persisted | 1 |
| 5 | Repeating `caprover_apply` was refused with `plan_not_applicable` | 1 |
| 6 | Explicit `caprover_reconcile` returned `verified`: version 2, exact requested image, one running task in slot 1, desired/running count 1 and HTTP 200 | 1 |
| 7 | `caprover_status` retained that dated receipt | 1 |

Requested image: `nginx@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de`. Baseline image: `nginx@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236`.

This CLI run went from uncertainty to verified runtime evidence. The separate earlier dispatcher run recorded an intermediate metadata/HTTP observation while old/new tasks overlapped; that event is not presented as part of this CLI trace.

## Source identity and data handling

All **10 of 10** entries in the trace's `plugin_source_sha256` were recomputed and matched the current **0.2.0** source/manifest bytes before publication preparation. They cover the root native adapter, package initializer, CLI, client, engine, diagnostics, guidance, demo, lab fixture and `plugin.yaml`. The matching root adapter includes the AIAgent call-order fix: middleware binds call identity, while the handler consumes the correlated post-approval ticket after the native gate.

The CLI banner reported an unknown version. A separate runtime source comparison matched the public Hermes commit [`f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab`](https://github.com/NousResearch/hermes-agent/commit/f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab), tagged `v0.22.6`, for these seven files: `run_agent.py`, `model_tools.py`, `agent/tool_executor.py`, `hermes_cli/plugins.py`, `hermes_cli/middleware.py`, `tools/approval.py` and `tools/approval_context.py`. This identifies the compared runtime paths without inferring a displayed release version or claiming that every runtime file was compared.

The public trace contains structured tool results, source hashes, process boundaries and deployment counters. Inspection found no raw credentials, authorization headers, private keys, environment definitions, raw CLI logs or response bodies. Only loopback target URLs appear. The trace checksum was preserved when copying it into this repository.

## Lab boundary and limits

The run used an explicitly authorized, isolated lab with a separate Docker daemon/Swarm. App creation, configuration and bootstrap deployment were separate setup operations, outside the one plugin POST count. An ephemeral lab CA was trusted only by the isolated child processes; TLS verification remained enabled. No production traffic or credentials were used.

After capture, the disposable lab container and its dedicated network were removed. No additional plugin deployment POST was issued. The trace is a recorded result, not a currently running demonstration endpoint.

Reproducing this CLI scenario requires a separately prepared disposable CapRover, fixed app/image configuration, an HTTPS lab proxy capable of dropping the acknowledgement, live model access and an explicitly authorized native UI response. The existing `demo/real_caprover_lab.py` documents the earlier dispatcher scenario; it is not a one-command reproduction of this live-model CLI session.

This is one integration run on one CapRover version. `verified` is a dated observation of task-image and HTTP evidence. It does not establish continuous availability, migration correctness, multi-node behavior, exclusive causation, distributed exactly-once delivery or production certification.
