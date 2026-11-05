# CapRover Operator

Use authenticated local `gh` for GitHub repository, issue and pull request operations.

Read `docs/implementation-contract.md` before changing the protocol. Keep the engine and HTTP client independent of Hermes. Use only public Hermes plugin APIs in the adapter. Never read credential files, bypass native approvals, retry a possible mutation automatically, or claim distributed exactly-once. Production operations are outside development and demo scope.

Run meaningful behavioral tests in an isolated environment. Keep all fixtures synthetic and all demo operations confined to a disposable lab. No telemetry or self-updater. Preserve MIT attribution when reusing source.
