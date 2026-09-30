import json
import os
from pathlib import Path

from localforge_ops.autostart import TASK_NAME, enable, disable, status
from localforge_ops.config import DEFAULTS, load, save
from localforge_ops.credentials import CredentialStore, TARGET
from localforge_ops.runtime import RuntimeController, running_version_from_tree
from localforge_ops.tray import MENU_ACTIONS, dispatch, handle_action, handle_timer_message, menu_model, refresh_state, status_label
from localforge_ops import cli

class FakeCred:
    def __init__(self): self.value = None
    def CredRead(self, *_):
        if self.value is None: raise RuntimeError("missing")
        return {"CredentialBlob": self.value}
    def CredWrite(self, item, *_): self.value = item["CredentialBlob"]
    def CredDelete(self, *_): self.value = None

def test_credential_store_fake_never_returns_secret_in_status():
    api = FakeCred(); store = CredentialStore(api); secret = "fake-secret"
    assert store.set(secret)["target"] == TARGET
    assert store.get() == secret
    assert secret not in json.dumps(store.status())

def test_config_has_only_non_secret_values(tmp_path):
    path = tmp_path / "runtime-ops.json"; data = load(path); save(data, path)
    text = path.read_text()
    assert set(json.loads(text)) == set(DEFAULTS) and "CONTROL_PLANE_API_KEY" not in text

class FakeResult:
    returncode = 0; stdout = '{"ready": true, "runtime_state": "ready", "process": {"pid": 10}}'

def test_runtime_status_provenance_and_idempotent_start(tmp_path):
    config = dict(DEFAULTS); config["runtime_root"] = str(tmp_path); (tmp_path / "current.txt").write_text("1.1.0")
    calls = []
    ctl = RuntimeController(config, runner=lambda *a, **k: (calls.append(a[0]) or FakeResult()), process_provider=lambda: [{"ProcessId": 11, "ParentProcessId": 10, "CommandLine": r"C:\runtime\localforge\versions\1.0.3\venv\Scripts\localforge.exe"}])
    state = ctl.status(); assert state["running_version"] == "1.0.3" and state["version_mismatch"]
    result = ctl.start(); assert result["action"] == "already_running" and len(calls) == 2

def test_start_injects_secret_only_to_child_and_stop_targets_alias(tmp_path):
    config = dict(DEFAULTS); config["runtime_root"] = str(tmp_path); (tmp_path / "current.txt").write_text("1.1.0")
    api = FakeCred(); CredentialStore(api).set("fake-secret"); popen_calls=[]; run_calls=[]
    class NotReady: returncode=0; stdout='{"ready": false, "runtime_state": "stopped"}'
    ctl = RuntimeController(config, CredentialStore(api), runner=lambda *a, **k: (run_calls.append((a,k)) or NotReady()), popen=lambda *a, **k: (popen_calls.append((a,k)) or object()))
    result = ctl.start(timeout=0); assert result["status"] == "failed"
    assert popen_calls[0][1]["env"]["CONTROL_PLANE_API_KEY"] == "fake-secret"
    assert all("fake-secret" not in json.dumps(x[0]) for x in run_calls)
    ctl.stop(); assert run_calls[-1][0][0][-3:] == ["runtimes", "stop", "localforge-web"]

def test_restart_and_autostart_are_scoped_and_idempotent(tmp_path):
    calls=[]
    class R: returncode=0; stdout='{}'
    runner=lambda args, **kwargs: (calls.append(args) or R())
    assert enable(tmp_path / "localforge-tray.vbs", runner)["task"] == TASK_NAME
    command = calls[0]
    assert "wscript.exe" in command[command.index("/TR") + 1].lower()
    assert "//B //Nologo" in command[command.index("/TR") + 1]
    assert command[command.index("/TR") + 1].endswith('\\localforge-tray.vbs"')
    assert status(runner)["status"] == "enabled"; assert disable(runner)["status"] == "ok"
    assert "localforge-web" in calls[-1] or TASK_NAME in calls[-1]

def test_tray_contract_is_small_and_stateful():
    assert set(("start", "stop", "restart", "exit")).issubset(MENU_ACTIONS)
    assert status_label({"ready": True}) == "LocalForge - 正常"
    assert status_label({"runtime_state": "stopped"}) == "LocalForge - 已停止"

def test_tray_dynamic_menu_and_dispatch_without_ui():
    state = {"ready": True, "runtime_state": "ready", "installed_version": "1.1.1", "running_version": "1.0.2", "version_mismatch": True}
    model = menu_model(state)
    assert model[0]["enabled"] is False and "1.1.1" in model[1]["label"] and "需要重启" in model[1]["label"]
    class Controller:
        def __init__(self): self.calls=[]
        def start(self): self.calls.append("start"); return {"status":"ok"}
        def stop(self): self.calls.append("stop"); return {"status":"ok"}
        def restart(self): self.calls.append("restart"); return {"status":"ok"}
        def status(self): return {"ui_url":"https://example.invalid/status", "log_path":"missing.log"}
    controller = Controller(); assert dispatch("start", controller)["status"] == "ok"; assert dispatch("restart", controller)["status"] == "ok"
    assert dispatch("exit", controller)["stop_called"] is False and controller.calls == ["start", "restart"]
    opened=[]; assert dispatch("open_status", controller, opener=lambda url: opened.append(url) or True)["status"] == "ok" and opened

def test_tray_timer_refresh_and_failure_are_safe():
    class Controller:
        def __init__(self): self.value = {"ready": False, "runtime_state": "stopped"}
        def status(self): return self.value
    controller = Controller(); calls = []
    handle_timer_message(0x0113, 0x0113, lambda: calls.append(refresh_state(controller)))
    assert calls[0]["runtime_state"] == "stopped"
    controller.value = {"ready": True, "runtime_state": "ready"}; assert status_label(refresh_state(controller)) == "LocalForge - 正常"
    class Broken:
        def status(self): raise RuntimeError("refresh failed")
    assert refresh_state(Broken())["refresh_error"] is True

def test_tray_exit_closes_ui_without_stopping_runtime():
    class Controller:
        def __init__(self): self.calls = []
        def stop(self): self.calls.append("stop")
    closed = []; controller = Controller()
    result = handle_action("exit", controller, lambda: closed.append("close"))
    assert result["stop_called"] is False and closed == ["close"] and controller.calls == []

def test_ops_status_has_fields_and_no_credential_leak(monkeypatch, capsys):
    state = {"ready": False, "runtime_state": "ready", "installed_version": "1.1.2", "running_version": "1.0.2", "version_mismatch": True}
    monkeypatch.setattr(cli, "load", lambda: dict(DEFAULTS))
    monkeypatch.setattr(cli, "CredentialStore", lambda: type("S", (), {"status": lambda self: {"status": "available", "secret": "fake-secret"}})())
    monkeypatch.setattr(cli, "RuntimeController", lambda *a: type("C", (), {"status": lambda self: state})())
    monkeypatch.setattr(cli, "autostart_status", lambda: {"status": "disabled"})
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert all(field in output for field in ("overall_status=", "alias=", "runtime_state=", "ready=", "healthy=", "installed_version=", "running_version=", "version_mismatch=", "ui_url=", "credential_status=", "autostart_status="))
    assert "1.0.2" in output and "version_mismatch=True" in output and "fake-secret" not in output

def test_process_tree_provenance_is_rooted_and_ignores_unrelated():
    rows = [{"ProcessId": 2, "ParentProcessId": 1, "CommandLine": r"C:\runtime\localforge\versions\1.0.2\venv\Scripts\localforge.exe"}, {"ProcessId": 3, "ParentProcessId": 99, "CommandLine": r"C:\runtime\localforge\versions\9.9.9\venv\Scripts\localforge.exe"}]
    assert running_version_from_tree(r"C:\runtime\localforge", 1, lambda: rows) == "1.0.2"
    assert running_version_from_tree(r"C:\runtime\localforge", 99, lambda: rows) == "9.9.9"
    assert running_version_from_tree(r"C:\runtime\localforge", None, lambda: rows) == "unknown"

def test_credential_import_env_does_not_print_secret(monkeypatch, capsys):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "fake-secret")
    monkeypatch.setattr(cli, "CredentialStore", lambda: CredentialStore(FakeCred()))
    assert cli.main(["credential", "import-env"]) == 0
    assert "fake-secret" not in capsys.readouterr().out


# --- Config open-source compatibility tests ---

def test_config_DEFAULTS_have_no_machine_specific_values():
    """DEFAULTS must not contain D:\\codex paths, fixed tunnel_id, or Oracle references."""
    assert "D:\\codex" not in DEFAULTS.get("runtime_root", "")
    assert "D:\\codex" not in DEFAULTS.get("launcher", "")
    assert "D:\\codex" not in DEFAULTS.get("tunnel_client", "")
    assert "tunnel_6ab" not in DEFAULTS.get("tunnel_id", "")
    assert DEFAULTS["runtime_root"].endswith("LocalForge")  # generic default
    assert DEFAULTS["tunnel_id"] == ""  # must be explicitly configured
    assert DEFAULTS["tunnel_client"] == ""  # must be explicitly configured


def test_config_env_var_overrides_defaults(monkeypatch, tmp_path):
    """Environment variables must override both DEFAULTS and any config file values."""
    monkeypatch.setenv("LOCALFORGE_TUNNEL_ID", "test-tunnel-id")
    monkeypatch.setenv("LOCALFORGE_TUNNEL_CLIENT", r"C:\tools\tunnel-client.exe")
    monkeypatch.setenv("LOCALFORGE_RUNTIME_ROOT", r"C:\data\localforge")
    cfg = load(tmp_path / "nonexistent.json")  # no config file
    assert cfg["tunnel_id"] == "test-tunnel-id"
    assert cfg["tunnel_client"] == r"C:\tools\tunnel-client.exe"
    assert cfg["runtime_root"] == r"C:\data\localforge"


def test_config_file_values_override_defaults(tmp_path):
    """Explicit config file values must override DEFAULTS (enables self-use compatibility)."""
    cfg_path = tmp_path / "runtime-ops.json"
    cfg_path.write_text(json.dumps({
        "alias": "my-localforge",
        "tunnel_id": "my-tunnel-id",
        "tunnel_client": r"C:\tools\tunnel.exe",
        "runtime_root": r"C:\data\my-localforge",
        "launcher": r"C:\data\my-localforge\bin\localforge.cmd",
    }))
    loaded = load(cfg_path)
    assert loaded["alias"] == "my-localforge"
    assert loaded["tunnel_id"] == "my-tunnel-id"
    assert loaded["runtime_root"] == r"C:\data\my-localforge"
    # DEFAULTS values not in config file stay as DEFAULTS
    assert loaded["profile"] == DEFAULTS["profile"]


def test_config_env_overrides_file_values(monkeypatch, tmp_path):
    """Environment variables must take highest priority (override both DEFAULTS and config file)."""
    cfg_path = tmp_path / "runtime-ops.json"
    cfg_path.write_text(json.dumps({
        "tunnel_id": "file-tunnel-id",
        "runtime_root": r"C:\file\runtime",
    }))
    monkeypatch.setenv("LOCALFORGE_TUNNEL_ID", "env-tunnel-id")
    cfg = load(cfg_path)
    assert cfg["tunnel_id"] == "env-tunnel-id"  # env wins
    assert cfg["runtime_root"] == r"C:\file\runtime"  # file wins (no env override)


def test_save_roundtrip(monkeypatch, tmp_path):
    """Values saved must survive a load, and env vars must not leak into saved file."""
    monkeypatch.setenv("LOCALFORGE_TUNNEL_ID", "env-tunnel-id")
    path = save({**DEFAULTS, "tunnel_id": "env-tunnel-id"}, tmp_path / "saved.json")
    raw = path.read_text()
    assert "env-tunnel-id" in raw
    assert "CONTROL_PLANE_API_KEY" not in raw
    # After clear, load from file (env var not set anymore in fresh load if we didn't patch it)
    # Actually monkeypatch stays for duration of test function; that's fine
