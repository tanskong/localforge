"""Testable pywin32 tray shell for LocalForge runtime operations."""
from __future__ import annotations

import ctypes
import os
import webbrowser
from pathlib import Path
from .runtime import RuntimeController

MENU_ACTIONS = ("status", "version", "tunnel", "start", "stop", "restart", "open_status", "open_log", "exit")

def status_label(state):
    if state.get("ready") or state.get("runtime_state") in {"ready", "connected"}: return "LocalForge - 正常"
    if state.get("runtime_state") in {"starting", "connecting"}: return "LocalForge - 启动中/重连中"
    if state.get("runtime_state") in {"stopped", "unknown"}: return "LocalForge - 已停止"
    return "LocalForge - 异常"

def menu_model(state):
    running = state.get("running_version", "unknown")
    installed = state.get("installed_version", "unknown")
    version = f"Installed：{installed} | Running：{running}"
    if state.get("version_mismatch"): version += "（需要重启）"
    return [
        {"id": "status", "label": status_label(state), "enabled": False},
        {"id": "version", "label": version, "enabled": False},
        {"id": "tunnel", "label": f"Tunnel：{'ready' if state.get('ready') else state.get('runtime_state', 'unknown')}", "enabled": False},
        {"separator": True},
        *({"id": action, "label": label, "enabled": True} for action, label in (("start", "启动 LocalForge"), ("stop", "停止 LocalForge"), ("restart", "重启 LocalForge"), ("open_status", "打开状态页面"), ("open_log", "打开日志"), ("exit", "退出托盘"))),
    ]

def dispatch(action, controller, opener=webbrowser.open, log_opener=os.startfile):
    if action == "start": return controller.start()
    if action == "stop": return controller.stop()
    if action == "restart": return controller.restart()
    if action == "open_status":
        state = controller.status(); url = state.get("ui_url")
        return {"status": "ok", "opened": opener(url)} if url else {"status": "unavailable", "reason": "ui_url_unavailable"}
    if action == "open_log":
        state = controller.status(); path = state.get("log_path") or state.get("process", {}).get("log_path")
        if path and Path(path).is_file(): log_opener(path); return {"status": "ok", "path": path}
        return {"status": "unavailable", "reason": "log_unavailable"}
    if action == "exit": return {"status": "ok", "action": "exit", "stop_called": False}
    return {"status": "ok", "state": controller.status()}

def handle_action(action, controller, close_ui):
    if action == "exit":
        close_ui()
        return {"status": "ok", "action": "exit", "stop_called": False}
    return dispatch(action, controller)

def refresh_state(controller):
    try:
        return controller.status()
    except Exception:
        return {"runtime_state": "unknown", "ready": False, "refresh_error": True}

def handle_timer_message(msg, timer_message, refresh):
    if msg == timer_message:
        return refresh()
    return None

def single_instance(name="LocalForgeTray"):
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    return handle, ctypes.windll.kernel32.GetLastError() != 183

def main():
    import win32api, win32con, win32gui
    controller = RuntimeController(); handle, first = single_instance()
    if not first: return
    class_name, wm_tray = "LocalForgeTrayWindow", win32con.WM_USER + 1
    menu = {"handle": None}
    def refresh(hwnd):
        state = refresh_state(controller)
        try: win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, (hwnd, 0, win32gui.NIF_TIP, wm_tray, 0, status_label(state)))
        except Exception: pass
        return state
    def wndproc(hwnd, msg, wparam, lparam):
        if msg == wm_tray and lparam == win32con.WM_RBUTTONUP:
            state = refresh(hwnd); menu["handle"] = win32gui.CreatePopupMenu()
            for item in menu_model(state):
                if item.get("separator"): win32gui.AppendMenu(menu["handle"], win32con.MF_SEPARATOR, 0, "")
                else: win32gui.AppendMenu(menu["handle"], win32con.MF_STRING | (0 if item["enabled"] else win32con.MF_GRAYED), MENU_ACTIONS.index(item["id"]) + 1, item["label"])
            win32gui.SetForegroundWindow(hwnd); win32gui.TrackPopupMenu(menu["handle"], win32con.TPM_LEFTALIGN, win32api.GetCursorPos()[0], win32api.GetCursorPos()[1], 0, hwnd, None)
        elif msg == win32con.WM_COMMAND:
            action = MENU_ACTIONS[(wparam & 0xFFFF) - 1]; handle_action(action, controller, lambda: win32gui.DestroyWindow(hwnd))
        elif msg == win32con.WM_TIMER: handle_timer_message(msg, win32con.WM_TIMER, lambda: refresh(hwnd))
        elif msg == win32con.WM_DESTROY: win32gui.PostQuitMessage(0)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
    wc = win32gui.WNDCLASS(); wc.hInstance = win32api.GetModuleHandle(None); wc.lpszClassName = class_name; wc.lpfnWndProc = wndproc; win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow(class_name, class_name, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
    icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION); win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (hwnd, 0, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP, wm_tray, icon, status_label(controller.status())))
    win32gui.SetTimer(hwnd, 1, 10000, None); win32gui.PumpMessages(); win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd,)); ctypes.windll.kernel32.CloseHandle(handle)

if __name__ == "__main__": main()
