# CapRover Operator

Small teams running open-source services on their own infrastructure need manageable deployments, so server administration does not become a second job. CapRover Operator brings one focused workflow into Hermes: deploy an immutable image to one owner-configured CapRover app through a saved plan and native human approval. If CapRover accepts the request just before the connection disappears, the plugin preserves uncertainty across restarts, blocks another deployment attempt for the same plan and uses read-only reconciliation to establish what was observed.

This is a development release; catalog acceptance is a separate step. The [recorded 0.3.0 validation](docs/real-cli-validation-v4.md) used native Hermes CLI and Desktop, a live model and CapRover 1.16.0 to update CyberChef 11.4.0 → 11.5.0: inline operation cards, a lost acknowledgement, real process restart, replay refusal and read-only reconciliation to verified task/HTTP evidence, with one plugin deployment POST. User-authorized lab automation operated native approval; the exact responding UI was not attributed, and no manual human keypress or synthetic approval callback is claimed. All 12 recorded source hashes match [the captured 0.3.0 revision](https://github.com/fmercurio/hermes-caprover-operator/commit/b1b8f0461651b84e5b00067437014a2fc4754741). Version 0.3.1 clarifies the safeguards' scope and adds an image-startup warning to the approval message; the recorded run predates those wording changes.

The earlier [0.2.0 CLI validation](docs/real-cli-validation.md) and its [trace](evidence/real-hermes-cli-trace.json) remain unchanged and separately attributed. The [public 0.3.0 trace](evidence/real-hermes-cli-trace-v4.json) documents its descriptive annotations and presentation-path sanitization while preserving the recorded operation facts and deployment counters.

The [protocol demos](#local-development-and-protocol-demo) use a synthetic API. The earlier [real CapRover dispatcher validation](docs/real-lab-validation.md) used a simulated host approval callback and separately captured metadata/HTTP appearing before runtime convergence. These evidence types are labeled individually.

## Why self-hosted

Autonomy becomes sustainable when a small team can reliably operate what it owns. Choosing where services run is valuable; so is understanding a deployment decision, reviewing it before execution and knowing what to do when a connection disappears. This plugin is designed to make that work accessible from an existing Hermes conversation through a focused, inspectable workflow.

[Hermes](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE) and this plugin are MIT-licensed. Their source can be inspected and adapted to a team's needs. The documented [Hermes plugin interfaces](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins) let the integration use host-owned credentials, native approval and durable plugin storage. An open license alone does not establish operational safety; the checks, failure behavior and evidence matter.

## Why CapRover

[CapRover](https://caprover.com/) is a self-hosted deployment platform for infrastructure its operator controls. It [manages Docker Swarm services](https://caprover.com/docs/fundamentals/how-caprover-works) and [accepts existing container images](https://caprover.com/docs/deployments#deploy-a-docker-image). That gives this release a concrete starting point: one existing app, one immutable image and observable deployment state. Moving stateful services still requires work on data, networking and recovery; a container image does not make those concerns disappear.

CapRover publishes its source under [Apache 2.0 with a project-specific appendix](https://github.com/caprover/caprover/blob/master/LICENSE), including additional conditions concerning paid features and redistribution. Review the complete terms separately from this plugin's MIT license.

The ecosystem already includes [better-caprover-mcp](https://github.com/guilhermebkel/better-caprover-mcp), [coolify-mcp](https://github.com/StuMason/coolify-mcp) and [Kastell](https://github.com/kastelldev/kastell), with existing deployment, preview or health workflows. This project's contribution is the particular native Hermes contract below: a saved baseline, fresh human approval, a durable claim before a possible write and explicit observation after uncertainty. The current release supports that bounded CapRover workflow.

## Workflow

1. **Plan:** read the configured app and save its version, image, replicas and keyed configuration fingerprint.
2. **Approve:** show the exact target, app, immutable image, baseline and plan identifier through Hermes' native human approval gate.
3. **Apply once:** durably claim the plan, re-read the baseline, reject drift, and make at most one deployment POST attempt for that plan.
4. **Reconcile:** observe deployment metadata, optional runtime task evidence and the configured HTTP endpoint. Preserve uncertainty when evidence is incomplete.

The ledger survives process restarts. A lost acknowledgement never makes an existing plan eligible for another POST. It does not provide distributed exactly-once delivery or remote compare-and-swap.

| Tool | Model arguments | Result |
| --- | --- | --- |
| `caprover_doctor` | None | Read-only setup and app checks, with suggested next steps; no plan or deployment request |
| `caprover_plan` | `image` | Saved state-bound plan; no deployment request |
| `caprover_apply` | `plan_id` | Fresh native human approval, preflight and one possible deployment attempt |
| `caprover_reconcile` | `plan_id` | Observation receipt; no deployment request |
| `caprover_status` | Optional `plan_id` | Local ledger inspection; no server request |

The model cannot choose a server, app, health URL or approval flag. Only an existing app and an OCI image reference containing `@sha256:<64 lowercase hex characters>` are accepted. There are no tools for shell execution, app creation/deletion, configuration changes, migrations or automatic rollback.

## Requirements and installation

- Hermes Agent **0.22.6 or newer**, with the public plugin approval hooks and tool execution middleware.
- Linux or macOS, Python **3.11+**, local POSIX storage supporting file permissions, file locking and SQLite.
- Hermes plugin isolation set to **`in_process`** and an interactive session with manual human approvals. Hosted plugin isolation is not supported by this release.
- A trusted HTTPS CapRover origin, an existing app and an API authentication token suitable for that server.

Install it by repository identifier:

```sh
hermes plugins install fmercurio/hermes-caprover-operator
```

Use Hermes' installer credential prompt for `CAPROVER_OPERATOR_TOKEN`. The manifest marks it as a secret. At request time this plugin asks `agent.secret_scope` for that credential; it never opens credential files, authentication databases or `.env` files itself. Do not put the token in a tool call or in the settings below.

Set the following non-secret owner configuration in the active Hermes profile, merging it with your existing configuration:

```yaml
plugins:
  enabled: [caprover-operator]
  isolation: in_process
  entries:
    caprover-operator:
      settings:
        url: https://captain.example.com
        app_name: example-app
        health_url: https://example-app.example.com/health
        plan_ttl_seconds: 300
approvals:
  mode: manual
```

`health_url` is optional. Plans expire after 30–3600 seconds; approval does not extend the configured lifetime. Use a dedicated Hermes profile for a different server or app.

Every apply needs a fresh human response for its correlated native call. Model arguments, smart/auxiliary-model decisions, YOLO mode, missing correlation identifiers, or missing hooks/middleware cannot create an execution permit. If your Hermes surface cannot provide this approval flow, apply refuses without deploying.

## First use

Start an interactive Hermes session after enabling the plugin and ask:

> Check my CapRover Operator setup and configured app without making changes.

Hermes can call `caprover_doctor` with no arguments, including before setup is complete. It checks owner settings and credential presence, then uses read requests for API/app, optional runtime and configured HTTP health evidence when possible. It creates no plan or persistent ledger. Credential presence and successful authenticated access are reported separately. A ready result means ready to **plan**; the doctor does not test native human approval, plugin isolation or permission to deploy.

Once the check identifies a usable app, provide an image reference with its actual registry digest and ask:

> Plan this immutable image for my configured app. Show me the target, current state, requested image, expiry and plan ID before applying it.

Review the saved intent and the native approval message when you choose to apply. If an acknowledgement is lost, use `caprover_reconcile` with the same plan ID. Inspect `caprover_status` for the local receipt when resuming a conversation. Suggested `guidance.next_step` entries describe an explicit next call; `automatic: false` means the plugin does not execute it on your behalf. Preserve the ledger while an outcome is uncertain.

## Operation cards

Workflow tools also return a read-only operation card: app and configured destination, previous and requested images, saved state, dated metadata/runtime/HTTP evidence and the next suggested tool. A planned operation remains a proposal; uncertain and metadata-only results stay visibly unverified. The card cannot approve or execute a deployment.

The plugin writes a private, immutable HTML snapshot and returns its exact `presentation.cards[].preview` directive. Hermes Desktop's public `::preview{file="…"}` surface can display it inline when the assistant includes that directive in its response. Ask Hermes to show the operation card after planning or reconciliation. CLI and other clients retain the same JSON receipt and selected card fields. HTML export failure is reported separately and never changes an operation's result.

Cards have no JavaScript, external assets or live refresh. They are historical observations, with full image digests and plan identity available in their details. Approval is described as a requirement; the card does not invent a persisted approval record or a server-request count.

## Understanding receipts

| Status | Evidence and next step |
| --- | --- |
| `planned` | Saved intent, not yet consumed |
| `consumed_drift` | App state changed since planning; no deployment POST was attempted |
| `expired` / `consumed_expired` | Plan expired; no deployment POST was attempted |
| `consumed_preflight_failed` | Required preflight evidence was unavailable; no deployment POST was attempted |
| `reconcile_required` | A request may have occurred or observations remain inconclusive; reconcile this operation |
| `deployment_recorded` | A newer version records the requested image, but positive HTTP evidence is absent |
| `metadata_http_observed` | Newer metadata records the requested image and the configured HTTP endpoint responded successfully; serving-image rollout is unproven |
| `verified` | Newer matching metadata, positive HTTP evidence, and optional runtime evidence all pass the rollout checks below |
| `observed_unhealthy` | Matching deployment metadata was observed, but health or replica evidence contradicts a healthy outcome |
| `superseded` | A later version with a different image was observed after this operation's deployment record |

CapRover may update deployment metadata before its Docker service converges. An HTTP 2xx response can come from the previous version. **Metadata plus HTTP alone cannot prove the new image is serving traffic.**

The client optionally reads CapRover's runtime app endpoint when the server exposes the supported schema. `verified` additionally requires a complete set of current tasks: distinct slots, running desired/current states, the exact requested repository and digest, and instance counts consistent with the configured replicas. If that endpoint is unavailable or its evidence is unsupported, reconciliation falls back to the metadata/HTTP statuses and sets `checks.runtime_rollout_proven` to `false`. The endpoint is a capability, not an assumption about every CapRover release.

Receipts are timestamped observations. Even `verified` is not proof of database migration correctness, every user request, a canary comparison or ongoing availability. It does not establish that this plugin alone caused the observed deployment.

You may reconcile `deployment_recorded`, `metadata_http_observed` and `observed_unhealthy` again to collect later evidence. Those observations cannot authorize another POST. The receipt retains a bounded history of observation summaries.

## Data and operational boundaries

- These safeguards apply to deployments made through this plugin's `caprover_apply` tool and its ledger. They do not govern independent actions through Hermes terminal or `execute_code` tools, other plugins, direct CapRover API calls, or external operators. Those paths require their own access controls and approvals.
- Requests go to the owner-configured CapRover origin. The fixed optional health URL receives a separate unauthenticated GET. There is no telemetry, self-updater or third-party service.
- CapRover's app definitions endpoint returns definitions for multiple apps, potentially including environment values. The client handles that response in memory, selects the configured app and stores a keyed HMAC fingerprint. Raw definitions, environment values, credentials, task logs and HTTP response bodies are not returned in receipts or written to the ledger.
- The app allowlist is enforced by this plugin; it does not narrow the server-side privileges of a CapRover token. Trust the configured origin and protect the credential accordingly.
- Plans, receipts, the SQLite ledger and a private fingerprint key live under Hermes' public per-plugin data directory. The final directory is owner-only (`0700`), state files are private (`0600`), and unsafe ownership or symlink paths are refused. Programs running as the same OS user are outside this storage boundary.
- Preflight is a separate read before a write. An external operator can change the app between those requests. There is no server-side conditional write or coordination with independent ledgers on other machines.
- Deploying an image runs that image's existing startup behavior, which may include migrations or other side effects. Returning to an older image does not restore environment values, volumes, schemas or data. This plugin performs no automatic rollback.
- Keep the ledger when an operation is uncertain. Reconciliation uses read requests only. Inconclusive observations keep the operation blocked; clearing files or repeatedly applying a new intent is not a recovery mechanism.

## Local development and protocol demo

The runtime package uses only the Python standard library. Pytest is a development dependency:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Tests cover baseline drift, expiry, concurrent/process-restarted operations, lost acknowledgements, replay refusal, bounded transport, secret redaction and approval provenance. Native Hermes integration tests skip when Hermes is unavailable. With Hermes installed in the test interpreter, they load the real PluginManager and dispatcher under an isolated `HERMES_HOME`, then use the real engine and HTTP client against the loopback fixture. The human response is an explicit host test callback. The copied plugin's test-only transport constructor enables HTTP for `127.0.0.1`; production plugin configuration requires HTTPS.

The command-line interface provides a disposable protocol demonstration and local receipt inspection:

```sh
python -m caprover_operator demo
python -m caprover_operator status \
  --data-dir /path/to/hermes-home/plugin-data/caprover-operator \
  --target https://captain.example.com --json
```

The protocol demo executes no container and contacts no real CapRover server. It demonstrates request accounting and ledger behavior. Use `demo --json` for machine-readable output or `demo --output PATH` to save it. Local status requires an existing ledger and accepts `--plan-id ID` to select a receipt; `--target` filters the configured origin without making a request. The CLI has no production apply command or approval shortcut. A real deployment needs the native Hermes tools and a separate disposable CapRover validation environment.

To record the native Hermes dispatch and approval events, run the following with an interpreter that can import Hermes:

```sh
python demo/native_hermes_demo.py --output /tmp/new-native-hermes-trace.json
```

This separate recording script labels its API as synthetic and its human responses as simulated host callbacks. Each tool runs in a fresh Hermes process against a disposable profile and the real engine/client. Its JSON trace includes the actual native approval message, public tool result and observed deployment POST count. It accepts no live server, credential, app or image options and creates the output file only if it does not already exist. This records a native integration demonstration, not a real human approval or real CapRover rollout.

For native admission checks, use the commands from your installed Hermes version against this repository:

```sh
hermes plugins validate .
hermes plugins doctor . --ci
```

These checks validate the package and registration; they do not establish a successful real deployment.

## Provenance and license

The project develops the bounded deployment and uncertainty-handling approach from the author's public [CapRover deployment and operations skills](https://github.com/fmercurio/clawdia-custom-skills/tree/f286115d051b811d98f7f33b20200a8f3338d07c/skills/devops). The native approval adapter, private durable ledger and restricted image deployment interface are implemented in this package. See [NOTICE](NOTICE) for source provenance and retained attribution.

Released under the [MIT License](LICENSE). Hermes and CapRover are separate projects; this package is not an endorsement by their maintainers.
