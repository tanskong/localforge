"""Content-free audit events. Failure to audit prevents release of tool results."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import hashlib

from server.security import SecurityError, reject_links


class Audit:
    def __init__(self, service_root: Path):
        self.directory = service_root / "logs"
        reject_links(self.directory)
        self.directory.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        build = hashlib.sha256((Path(__file__).resolve().parents[1] / "adapters" / "baseline.py").read_bytes()).hexdigest()[:12]
        self.record("server_start", {"baseline_build": build}, {"status": "ok"}, 0)

    def record(self, tool: str, parameters: dict, result: dict, size: int) -> None:
        event = {"time": datetime.now(timezone.utc).isoformat(), "tool": tool, "parameters": parameters, "success": result.get("status") != "error", "return_bytes": size, "truncated": bool(result.get("truncated")), "security_blocked": bool(result.get("security_blocked"))}
        # Daily audit files; file contents and raw paths/queries are never stored.
        path = self.directory / ("audit-" + event["time"][:10] + ".jsonl")
        with self.lock:
            reject_links(path)
            if path.exists() and (not path.is_file() or path.stat().st_nlink > 1):
                raise SecurityError("Unsafe audit destination")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
