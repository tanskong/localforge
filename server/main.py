"""LocalForge MCP v2 STDIO entrypoint."""
from pathlib import Path
import fnmatch
from importlib.metadata import PackageNotFoundError, version as package_version
import tomllib
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from server.service import Service


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
EXEC = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


def _validate(service: Service, root: str, commands: list[str],
              allowed_patterns: list[str] | None, denied_patterns: list[str] | None,
              timeout_seconds: int) -> dict[str, Any]:
    """Run commands against a baseline captured by this call, then evaluate scope."""
    if not isinstance(commands, list) or any(not isinstance(item, str) or not item.strip() for item in commands):
        return {"status": "error", "error": "invalid_command", "summary": "commands must be non-empty strings",
                "commands": [], "scope": {"status": "not_run", "passed": False}, "warnings": ["invalid_commands"], "truncated": False}

    baseline = service.call("capture_git_baseline", {"root": root})
    results = []
    for command in commands:
        result = service.call("run_process", {"root": root, "command": command, "shell": True, "timeout_seconds": timeout_seconds})
        results.append({"command": command, **result})

    after_status = service.call("git_status", {"path": root})

    if baseline.get("status") != "ok" or not isinstance(baseline.get("snapshot_id"), str):
        scope = {"status": baseline.get("status", "error"), "passed": False,
                 "reason": baseline.get("error", "baseline_unavailable"), "snapshot_id": baseline.get("snapshot_id")}
    else:
        scope = service.call("verify_change_scope", {"root": root, "snapshot_id": baseline["snapshot_id"],
                                                       "allowed_patterns": allowed_patterns or ["**"],
                                                       "denied_patterns": denied_patterns or []})
        scope["passed"] = scope.get("status") == "ok" and not scope.get("out_of_scope_changes")
        if not scope["passed"] and scope.get("status") == "ok":
            scope["reason"] = "out_of_scope_changes"
    current_files = sorted({item.get("path", "") for item in after_status.get("files", []) if item.get("path")})
    current_out = [path for path in current_files if not any(fnmatch.fnmatch(path, pattern) for pattern in (allowed_patterns or ["**"])) or any(fnmatch.fnmatch(path, pattern) for pattern in (denied_patterns or []))]
    scope["current"] = {"files": current_files, "out_of_scope_changes": current_out}

    command_failures = [item for item in results if item.get("status") != "ok" or item.get("exit_code") not in (None, 0)]
    commands_passed = not command_failures
    scope_passed = scope.get("passed") is True
    warnings = []
    if command_failures:
        warnings.append("validation_command_failed")
    if not scope_passed:
        warnings.append("scope_validation_failed")
    status = "ok" if commands_passed and scope_passed else "failed"
    return {
        "status": status,
        "summary": f"commands={'passed' if commands_passed else 'failed'}; scope={'passed' if scope_passed else 'failed'}",
        "commands": {"items": results, "passed": commands_passed, "failed": len(command_failures)},
        "scope": scope,
        "warnings": warnings,
        "truncated": any(item.get("truncated", False) for item in results),
    }


def build_server(service_root: Path) -> MCPServer:
    service = Service(service_root)
    mcp = MCPServer(
        "LocalForge",
        description="Local coding and automation runtime for trusted Windows development machines.",
        instructions="GPT is the decision-maker; LocalForge provides local execution, state, and evidence. Dev execution is not a sandbox.",
        version=_runtime_version(),
    )

    def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = service.call(tool, args)
        result.setdefault("summary", result.get("message", result.get("status", "completed")))
        result.setdefault("warnings", [])
        result.setdefault("timings", {"total_ms": result.get("duration_ms", 0)})
        result.setdefault("truncated", False)
        return result

    @mcp.tool(annotations=READ, structured_output=True)
    def project_context(path: str, depth: int = 2) -> dict[str, Any]:
        """Return high-signal project structure, Git state, docs, and current work handoff."""
        overview = call("project_overview", {"path": path, "depth": depth})
        work = call("task_status", {"root": path})
        return {**overview, "work": work, "summary": "Project context and current work status"}

    @mcp.tool(annotations=READ, structured_output=True)
    def list_directory(path: str, depth: int = 2, limit: int = 200) -> dict[str, Any]:
        """List a bounded absolute local directory."""
        return call("list_directory", {"path": path, "depth": depth, "limit": limit})

    @mcp.tool(annotations=READ, structured_output=True)
    def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
        """Read an allowed text file or line range."""
        return call("read_file", {"path": path, "start_line": start_line, "end_line": end_line})

    @mcp.tool(annotations=READ, structured_output=True)
    def search_code(path: str, query: str, mode: str = "text", file_glob: str | None = None, max_results: int = 50) -> dict[str, Any]:
        """Search text or symbols below an absolute project directory."""
        if mode == "symbol":
            return call("symbol_search", {"root": path, "symbol": query, "max_results": max_results})
        return call("search_text", {"path": path, "query": query, "file_glob": file_glob, "max_results": max_results})

    @mcp.tool(annotations=WRITE, structured_output=True)
    def apply_patch(root: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """Create, update, or delete multiple project files with conflict-aware preflight."""
        return call("apply_changes", {"root": root, "changes": changes})

    @mcp.tool(annotations=EXEC, structured_output=True)
    def shell(root: str, executable: str = "", args: list[str] | None = None, command: str | None = None,
              cwd: str | None = None, env: dict[str, str] | None = None, shell: bool = False,
              background: bool = False, timeout_seconds: int = 120) -> dict[str, Any]:
        """Run a non-interactive local command; cwd must be an absolute local path when provided."""
        return call("run_process", {"root": root, "executable": executable, "args": args or [], "command": command,
                    "cwd": cwd, "env": env, "shell": shell, "background": background,
                    "timeout_seconds": timeout_seconds})

    @mcp.tool(name="process_status", annotations=READ, structured_output=True)
    def process_status(process_id: str) -> dict[str, Any]:
        """Read status of a LocalForge background child process."""
        return call("process_status", {"process_id": process_id})

    @mcp.tool(name="process_stop", annotations=WRITE, structured_output=True)
    def process_stop(process_id: str) -> dict[str, Any]:
        """Stop a LocalForge background child process."""
        return call("stop_process", {"process_id": process_id})

    @mcp.tool(annotations=EXEC, structured_output=True)
    def run_python(root: str, code: str, mode: str = "dev", purpose: str | None = None, timeout_seconds: int = 20) -> dict[str, Any]:
        """Run Python automation in dev mode or restricted analysis in safe mode."""
        return call("run_python", {"root": root, "code": code, "mode": mode, "purpose": purpose, "timeout_seconds": timeout_seconds})

    @mcp.tool(annotations=READ, structured_output=True)
    def git_status(path: str) -> dict[str, Any]:
        """Return structured branch, HEAD-related dirty, staged, and untracked Git state."""
        return call("git_status", {"path": path})

    @mcp.tool(annotations=READ, structured_output=True)
    def git_diff(path: str, base: str | None = None, head: str | None = None) -> dict[str, Any]:
        """Inspect working-tree/staged changes or compare two revisions."""
        if base is not None or head is not None:
            if not isinstance(base, str) or not isinstance(head, str):
                return {"status": "error", "error": "invalid_arguments", "summary": "base and head are required together"}
            return call("git_compare", {"path": path, "base": base, "head": head})
        return call("git_diff", {"path": path})

    @mcp.tool(annotations=READ, structured_output=True)
    def git_log(path: str, limit: int = 20) -> dict[str, Any]:
        """Return bounded recent commit history."""
        return call("git_history", {"path": path, "limit": limit})

    @mcp.tool(annotations=READ, structured_output=True)
    def git_show(path: str, revision: str) -> dict[str, Any]:
        """Show one Git revision with bounded patch output."""
        return call("git_show", {"path": path, "revision": revision})

    @mcp.tool(annotations=READ, structured_output=True)
    def work_status(root: str) -> dict[str, Any]:
        """Read current TASK/RESULT handoff files and status summary."""
        return call("task_status", {"root": root})

    @mcp.tool(annotations=WRITE, structured_output=True)
    def work_update(root: str, content: str, target: str = "task") -> dict[str, Any]:
        """Update the local ignored handoff with task, result, or checkpoint content."""
        if target not in {"task", "result", "checkpoint"}:
            return {"status": "error", "error": "invalid_target", "summary": "target must be task, result, or checkpoint"}
        return call("work_update", {"root": root, "content": content, "target": target})

    @mcp.tool(annotations=EXEC, structured_output=True)
    def validate(root: str, commands: list[str],
                 allowed_patterns: list[str] | None = None, denied_patterns: list[str] | None = None,
                 timeout_seconds: int = 120) -> dict[str, Any]:
        """Run explicit commands and a reliable baseline-to-post-command scope check."""
        return _validate(service, root, commands, allowed_patterns, denied_patterns, timeout_seconds)

    return mcp

def _runtime_version() -> str:
    project = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if project.is_file():
        with project.open("rb") as stream:
            return tomllib.load(stream)["project"]["version"]
    try:
        return package_version("localforge")
    except PackageNotFoundError:
        return "0.0.0.dev"


def main() -> None:
    build_server(Path(__file__).resolve().parent.parent).run(transport="stdio")


if __name__ == "__main__":
    main()
