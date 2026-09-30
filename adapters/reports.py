"""Existing report metadata only; never invokes a test runner."""
from datetime import datetime, timezone
import json
from pathlib import Path

from defusedxml import ElementTree

from server.security import MAX_FILE, SecurityError, read_text, safe_path

REPORT_CANDIDATES = (
    "reports/latest-test.json", "reports/junit.xml", "reports/pytest.xml",
    "junit.xml", "pytest.xml", "pytest-report.json", "report.json",
)


def test_status(root: Path) -> dict:
    reports = []
    blocked = False
    candidates = list(REPORT_CANDIDATES)
    for base in (root / "reports", root / "logs"):
        if base.is_dir():
            for path in list(base.rglob("*pytest*.xml"))[:30] + list(base.rglob("*junit*.xml"))[:30]:
                candidates.append(path.relative_to(root).as_posix())
    for path in list(root.glob("*pytest*.xml"))[:30] + list(root.glob("*junit*.xml"))[:30]:
        candidates.append(path.relative_to(root).as_posix())
    for relative in dict.fromkeys(candidates):
        try:
            path = safe_path(root, relative)
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            content = read_text(root, relative, limit=MAX_FILE)
            entry = {"path": relative, "modified_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(), "age_seconds": max(0, int(datetime.now(timezone.utc).timestamp() - mtime)), "status": "unknown", "truncated": content["truncated"], "mtime": mtime}
            if content["status"] != "ok" or content["truncated"]:
                entry["reason"] = "unreadable_or_oversized_report"
            else:
                try:
                    if path.suffix.lower() == ".xml":
                        xml_root = ElementTree.fromstring(content["text"])
                        if xml_root.tag not in {"testsuite", "testsuites"}:
                            raise ValueError("Not JUnit")
                        cases = list(xml_root.iter("testcase"))
                        if cases:
                            counts = {"tests": len(cases), "failures": sum(c.find("failure") is not None for c in cases), "errors": sum(c.find("error") is not None for c in cases), "skipped": sum(c.find("skipped") is not None for c in cases)}
                        else:
                            suites = [s for s in xml_root.iter("testsuite") if s.find("testsuite") is None]
                            if not suites and xml_root.tag == "testsuites":
                                suites = [xml_root]
                            counts = {k: sum(int(s.get(k, "0")) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
                    elif path.suffix.lower() == ".json":
                        data = json.loads(content["text"])
                        summary = data.get("summary", {})
                        counts = {"tests": int(summary["total"]), "failures": int(summary.get("failed", 0)), "errors": int(summary.get("error", 0)), "skipped": int(summary.get("skipped", 0))}
                    else:
                        raise ValueError("Unsupported format")
                    if any(v < 0 for v in counts.values()) or sum(counts[k] for k in ("failures", "errors", "skipped")) > counts["tests"]:
                        raise ValueError("Invalid report counters")
                    entry.update(counts)
                    entry["status"] = "failed" if counts["failures"] or counts["errors"] else ("passed" if counts["tests"] > counts["skipped"] else "unknown")
                except Exception:
                    entry["reason"] = "invalid_or_unsupported_report"
            reports.append(entry)
        except SecurityError:
            blocked = True
        except OSError:
            reports.append({"path": relative, "status": "unavailable", "mtime": 0})
    reports.sort(key=lambda r: r["mtime"], reverse=True)
    for item in reports:
        item.pop("mtime")
    return {"status": reports[0]["status"] if reports else "no_report", "latest": reports[0] if reports else None, "reports": reports, "freshness": "Report timestamp only; HEAD not verified", "security_blocked": blocked, "truncated": any(r.get("truncated") for r in reports)}
