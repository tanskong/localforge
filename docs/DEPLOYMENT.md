# LocalForge Deployment and Release

LocalForge follows a three-part architecture:

1. **Development source repository** — where you clone, edit, test, and build.
2. **Release storage server** — stores immutable wheel releases only. Can be any SSH-accessible host (Oracle, Azure VM, DigitalOcean, etc.).
3. **Local runtime** — installed on the end-user machine, isolated from source.

## Release-First Execution Contract

Source is for development. Release artifact is for execution. The formal loop is:

```
source change → tests → version → commit → wheel build → server publish → download + SHA256/manifest verification → independent install → installed MCP black-box → atomic current.txt switch → stable launcher
```

Do not use `server.main` or an editable install as the daily execution path. Source direct-run is for development and testing only.

### Build

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py
```

Verifies the working tree is clean, builds a wheel, and emits SHA256SUMS plus a JSON manifest. The manifest records product, version, SHA256, git commit, build time, and Python requirements. Integrity is verified through SHA256/manifest matching — there is no cryptographic signing step.

### Publish

```powershell
.\.venv\Scripts\python.exe scripts\publish_release.py `
  --host <release-user>@<release-host> `
  --key <ssh-key-path>
```

The publisher uses `IdentitiesOnly=yes`, strict host-key checking, a temporary upload directory, SHA256 verification, immutable version directories, and an atomic `latest.json` replacement. It is restricted to `/opt/localforge`; it does not touch other services on the release host.

You can host releases on any SSH-accessible machine. Oracle Cloud is one common choice; substitute your preferred provider.

### Install

```powershell
.\scripts\install_release.ps1 -HostName <release-user>@<release-host> `
  -KeyPath <ssh-key-path> `
  -RuntimeRoot "%LOCALAPPDATA%\LocalForge" `
  -Version latest
```

The installer accepts `-Version latest` or a specific version such as `-Version 1.0.1`, downloads the matching manifest and wheel, verifies product/version/SHA256, creates a versioned venv, and runs an installed black-box smoke. Only after the 16-tool MCP smoke passes does it atomically update `current.txt` and write `bin\localforge.cmd`. The launcher reads `current.txt`, passes through STDIO arguments, and fails closed when the current version is missing.

- Existing versions remain available for rollback.
- A failed smoke does not switch the current version.
- The installer does not clone source, modify other applications, or change firewall rules.

## Release Storage Notes

The release server only stores artifacts (`localforge-*.whl`, `manifest.json`, `SHA256SUMS`). It does **not**:

- Clone or serve source code
- Execute user-local projects
- Run LocalForge itself
- Store credentials, API keys, or user data

Each release version directory is immutable — once published, the files in that version are never overwritten. Rollback is simply switching `current.txt` to an earlier version string.

## Runtime Root Choices

| Scenario | RuntimeRoot |
|---|---|
| Normal user install | `%LOCALAPPDATA%\LocalForge` (default) |
| Developer self-use | Any explicit path you prefer |
| Container/CI | Container-local path |

Always verify the resolved RuntimeRoot after installation. The install script prints it on success.

## Security Notes

- Private key contents never belong in this repository, release artifacts, or logs.
- Keys, tokens, and passwords must be passed only through CLI arguments (`--key`), environment variables, or Windows Credential Manager on the local machine.
- The tunnel-control-plane API key is stored per-user in Windows Credential Manager as `LocalForge/control-plane/localforge-web`. It is injected only into the tunnel-client child environment as `CONTROL_PLANE_API_KEY`; it is never written to files, YAML, JSON, BAT, or audit logs.
- The stable launcher (`bin\localforge.cmd`) reads only `current.txt` and the versioned venv path. It contains no credentials.
