# LocalForge 基线验收说明

本文件描述 LocalForge 已关闭 Gate 0 基线的可复核验收方式，不把历史报告当作当前实时状态。

## 当前基线

- 入口：`server.main:main` 或 `localforge` console script。
- SDK：`mcp==2.2.0`，使用 `mcp.server.MCPServer` 高层 API。
- Transport：MCP STDIO。
- `dev`：可信本机开发执行，支持授权的文件写入、进程和项目命令。
- `safe`：`run_python` 的受限分析模式。
- 公共工具面为 16 个，集中在 project context、文件读取/编辑、shell/process、run_python、Git、work handoff 和 validate；旧能力保留在内部 service/adapters。
- Gate 0：PASS / Closed；当前工作方向为 Gate 1 — Codex-level Coding Runtime。
- `validate` 同时检查当前 working tree scope evidence；调用前已存在的越界改动不能被新的内部 baseline 隐藏。

## 验收命令

```powershell
.\.venv\Scripts\python.exe -m compileall -q server adapters scripts tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```

`tests/test_protocol.py` 提供真实 STDIO protocol test；它与完整 pytest 一起执行。editable install 后应能生成 `localforge` 入口并启动到 STDIO 初始化路径。真实 ChatGPT/Tunnel/Provider 会话未由本地验收代替，须单独取得 live evidence。
