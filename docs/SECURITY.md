# Security model

LocalForge is for one trusted developer on their own Windows machine. Its aim is to prevent cheap, high-impact mistakes, not to turn a personal development helper into an adversarial-code execution platform.

## Dev mode

Dev mode deliberately allows project writes, subprocesses, shell/PowerShell/cmd when explicitly requested, package managers, Git local operations, normal inherited network access and project configuration including `.env`. It runs with the current Windows user and is not a VM, container, zero-trust boundary, or protection from code the user asked it to run.

The service canonicalizes final targets so ordinary symlinks/junctions to normal development directories work. It rejects paths whose resolved target is a machine credential store (SSH private keys, browser login/Cookie stores, Windows credential/SAM/SECURITY data, tunnel-client credentials) or a Windows core area. This is location/context based: names such as `tokenizer.py`, `credentials_schema.json`, `.env`, `local.db`, and `config.local.*` in a project are allowed.

## Safe mode

`run_python(mode="safe")` retains the previous restricted Python analysis policy. It rejects process/network/write escapes and uses an isolated workspace for its optional workspace compatibility mode. Safe restrictions never constrain dev mode.

## Resilience and observability

Each tool catches failures so one exception or child crash does not stop the STDIO server. Foreground process timeouts kill the child and output is bounded; background children have explicit status/stop operations. Audit logs capture useful operation metadata but omit content and environment values. Output redaction masks likely secret values while preserving function names, tests, identifiers, patch shape and control flow.

Tunnel reconnect and ChatGPT session reconnect are connection-layer concerns; LocalForge does not claim to solve them. Stable Project MCP and its Tunnel/ChatGPT configuration are outside LocalForge's writable scope.
