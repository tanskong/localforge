"""Fixed read-only Git operations, bounded output, no shell or external helpers."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

from server.security import MAX_DIFF, SCAN_LIMIT, SecurityError, redact, reject_links, safe_path


class GitError(RuntimeError):
    pass


class GitReader:
    def __init__(self, root: Path):
        self.root = root
        self.executable = shutil.which("git")
        if os.name == "nt" and self.executable:
            native = Path(self.executable).parent.parent / "mingw64" / "bin" / "git.exe"
            if native.is_file():
                # Bypass Git for Windows' cmd launcher so timeout/kill owns the actual process.
                self.executable = str(native)
        self._checked = False

    def check(self) -> None:
        if self._checked:
            return
        root = self.root
        reject_links(root)
        meta = root / ".git"
        if not meta.is_dir():
            raise GitError("not_git_or_unsupported_worktree")
        if not self.executable:
            raise GitError("git_not_installed")
        # Fail closed on indirection, partial clones and linked Git metadata.
        count = 0
        for parent, dirs, files in os.walk(meta, followlinks=False):
            reject_links(Path(parent))
            for name in dirs + files:
                count += 1
                if count > 30000:
                    raise GitError("git_metadata_scan_limit")
                path = Path(parent) / name
                reject_links(path)
                if path.is_file() and path.stat().st_nlink > 1:
                    raise SecurityError("Hard linked Git metadata blocked")
        for relative in ("commondir", "objects/info/alternates", "objects/info/http-alternates", "info/grafts"):
            if (meta / relative).exists():
                raise SecurityError("External Git object or directory indirection blocked")
        cfg = meta / "config"
        if cfg.exists():
            if cfg.stat().st_size > 65536:
                raise SecurityError("Git configuration too large")
            raw = cfg.read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?im)^\s*\[\s*(?:include|includeIf|extensions|filter)\b|^\s*(?:worktree|alternaterefscommand|sshcommand|promisor|partialclonefilter)\s*=", raw):
                raise SecurityError("Git external configuration, filters or partial clone blocked")
        self._checked = True

    def _run(self, operation: str, *, limit: int = 20, path: str | None = None, staged: bool = False, cap: int = 1024 * 1024) -> tuple[str, bool]:
        self.check()
        commands = {
            "status": ["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=all"],
            "branch": ["branch", "--show-current"],
            "history": ["log", f"-{limit}", "--format=%x1e%h%x1f%cI%x1f%s", "--numstat", "--no-renames", "--no-show-signature", "--no-notes", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"],
            "diff": ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=all", "--no-color", "--unified=3"],
        }
        if operation not in commands or type(limit) is not int or not 1 <= limit <= 50:
            raise SecurityError("Git operation outside fixed whitelist")
        args = commands[operation]
        if operation == "diff":
            if path is None:
                raise SecurityError("Diff requires an approved file")
            safe = self.root.joinpath(*path.split("/"))
            if not safe.resolve().is_relative_to(self.root):
                raise SecurityError("Diff path leaves root")
            if staged:
                args.append("--cached")
            args.extend(["--", path])
        # No caller-provided command strings, Git environment, pager, hooks, filters or network.
        env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT", "LANG"}}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_LITERAL_PATHSPECS": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_PAGER": "", "LC_ALL": "C"})
        command = [self.executable, "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull, "-c", "core.excludesFile=" + os.devnull, "-c", "diff.external=", "-c", "diff.orderFile=" + os.devnull, "-c", "diff.ignoreSubmodules=all", "-c", "status.submoduleSummary=false", "-c", "protocol.allow=never", "-c", "log.showSignature=false", "-c", "core.quotePath=false", "-C", str(self.root), *args]
        # Git must never inherit the live MCP input pipe (Git for Windows wrappers can wait on it).
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = bytearray()
        overflow = False

        def collect() -> None:
            nonlocal overflow
            assert proc.stdout is not None
            while block := proc.stdout.read(8192):
                remaining = cap - len(output)
                output.extend(block[:remaining])
                if len(block) > remaining:
                    overflow = True
                    proc.kill()
                    break

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise GitError("git_timeout")
        finally:
            reader.join(timeout=2)
            if proc.stdout:
                proc.stdout.close()
        if proc.returncode and not overflow:
            raise GitError("git_command_unavailable_or_no_commits")
        data = bytes(output)
        if overflow:
            data = data.rpartition(b"\x00" if operation == "status" else b"\n")[0]
        return data.decode("utf-8", errors="replace"), overflow

    def status(self) -> dict:
        raw, truncated = self._run("status")
        branch, _ = self._run("branch")
        files = []
        blocked = 0
        chunks = raw.split("\x00")
        i = 0
        while i < len(chunks):
            entry = chunks[i]
            i += 1
            if not entry:
                continue
            code, name = entry[:2], entry[3:]
            if "R" in code or "C" in code:
                i += 1
            try:
                safe_path(self.root, name.rstrip("/"))
                files.append({"path": name, "index": code[0], "worktree": code[1]})
            except (SecurityError, OSError):
                blocked += 1
        return {"status": "ok", "branch": branch.strip() or "detached_HEAD", "dirty": bool(raw) or truncated, "files": files[:SCAN_LIMIT], "filtered_files": blocked, "security_blocked": blocked > 0, "truncated": truncated or len(files) > SCAN_LIMIT}

    def history(self, limit: int) -> dict:
        raw, truncated = self._run("history", limit=limit)
        commits = []
        for section in raw.split("\x1e")[1:]:
            header, _, body = section.partition("\n")
            fields = header.split("\x1f", 2)
            if len(fields) != 3:
                continue
            changes = []
            filtered = 0
            for row in body.splitlines():
                cols = row.split("\t", 2)
                if len(cols) != 3:
                    continue
                add, remove, name = cols
                # Quoted unusual filenames are omitted rather than interpreted.
                try:
                    if name.startswith('"'):
                        raise SecurityError("Quoted filename omitted")
                    safe_path(self.root, name)
                except (SecurityError, OSError):
                    filtered += 1
                    continue
                changes.append({"path": name, "added": int(add) if add.isdigit() else None, "deleted": int(remove) if remove.isdigit() else None})
            commits.append({"hash": fields[0], "time": fields[1], "message": redact(fields[2])[:1000], "file_count": len(changes) + filtered, "filtered_files": filtered, "changes": changes[:160], "truncated": len(changes) > 160})
        return {"status": "ok", "commits": commits, "truncated": truncated or any(c["truncated"] for c in commits), "security_blocked": any(c["filtered_files"] for c in commits)}

    def diff(self) -> dict:
        status = self.status()
        sections = {"staged": [], "unstaged": []}
        remaining = MAX_DIFF
        truncated = status["truncated"]
        untracked = []
        for entry in status["files"]:
            if entry["index"] == "?":
                untracked.append(entry["path"])
                continue
            if len(sections["staged"]) + len(sections["unstaged"]) >= 80 or remaining <= 0:
                truncated = True
                break
            for stage, field in ((True, "index"), (False, "worktree")):
                if entry[field] == " ":
                    continue
                if remaining <= 0:
                    truncated = True
                    break
                raw, cut = self._run("diff", path=entry["path"], staged=stage, cap=256 * 1024)
                clean = redact(raw)
                truncated |= cut or len(clean) > remaining
                sections["staged" if stage else "unstaged"].append({"path": entry["path"], "diff": clean[:remaining]})
                remaining -= min(len(clean), remaining)
        return {"status": "ok", **sections, "untracked": untracked[:160], "untracked_content": "not_read", "filtered_files": status["filtered_files"], "security_blocked": status["security_blocked"], "truncated": truncated or len(untracked) > 160}

    def _git(self, *args: str, cap: int = MAX_DIFF) -> tuple[str, bool]:
        """Read-only Git inspection used by the developer audit tools."""
        self.check()
        env = os.environ.copy()
        env.update({"GIT_PAGER": "", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        proc = subprocess.run([self.executable, "-C", str(self.root), "--no-pager", *args], stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=20)
        if proc.returncode: raise GitError("git_command_unavailable_or_revision_not_found")
        data = proc.stdout.decode("utf-8", errors="replace")
        return data[:cap], len(data) > cap

    def show(self, revision: str) -> dict:
        raw, cut = self._git("show", "--format=%H%x1f%cI%x1f%s", "--stat", "--patch", "--no-ext-diff", "--no-textconv", revision)
        header, _, patch = raw.partition("\n")
        fields = header.split("\x1f", 2)
        return {"status":"ok", "revision":revision, "metadata":{"hash":fields[0],"time":fields[1],"message":redact(fields[2])} if len(fields)==3 else {}, "patch":redact(patch), "truncated":cut}

    def compare(self, base: str, head: str) -> dict:
        raw, cut = self._git("diff", "--no-ext-diff", "--no-textconv", "--stat", "--patch", base, head)
        names, name_cut = self._git("diff", "--name-status", base, head, cap=128000)
        paths=[]
        for line in names.splitlines():
            pieces=line.split("\t")
            if len(pieces)>=2: paths.append(pieces[-1])
        return {"status":"ok","base":base,"head":head,"changed_files":paths,"patch":redact(raw),"truncated":cut or name_cut}

    def blame(self, path: str, start_line: int | None=None, end_line: int | None=None) -> dict:
        safe_path(self.root, path)
        args=["blame","--line-porcelain"]
        if start_line: args.extend(["-L", f"{start_line},{end_line or start_line}"])
        args.extend(["--",path]); raw,cut=self._git(*args)
        entries=[]; current={}
        for line in raw.splitlines():
            if re.match(r"^[0-9a-f]{40} ",line):
                if current: entries.append(current)
                p=line.split(); current={"commit":p[0][:12],"line":int(p[2])}
            elif line.startswith("author "): current["author"]=line[7:]
            elif line.startswith("author-time "): current["time"]=line[12:]
            elif line.startswith("\t"): current["text"]=redact(line[1:])
        if current: entries.append(current)
        return {"status":"ok","path":path,"lines":entries,"truncated":cut}
