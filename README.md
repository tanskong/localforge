# LocalForge

LocalForge 是一个本地开发自动化 Runtime：通过 MCP STDIO transport 为 LLM Agent 提供项目上下文、文件读写、shell、Python 自动化、Git 审计和测试能力。

它是执行层，不内置另一个 LLM Agent。

## 它做什么

LocalForge 以一个独立的 Python 进程运行，通过标准 MCP 协议（JSON-RPC over STDIO）对外暴露一组聚焦的开发工具。你可以把它接入任何 MCP 兼容的客户端——Trae、OpenCode、Claude Desktop、VS Code extensions，或你自己的 Agent 系统。

```
LLM Agent ←─MCP STDIO─→ LocalForge ←─本地文件/进程/Git/Windows API─→ 你的项目
```

核心设计约束：**安全边界是本机当前用户**，不是 VM、container 或零信任沙箱。Dev 模式允许对授权项目的正常文件操作、进程执行和继承网络；Safe 模式为 `run_python` 提供受限分析环境。详见 [docs/SECURITY.md](docs/SECURITY.md)。

## 当前工具（16）

| 分类 | 工具 |
|---|---|
| 项目上下文 | `project_context`, `list_directory`, `read_file`, `search_code` |
| 文件操作 | `apply_patch` |
| Shell/进程 | `shell`, `process_status`, `process_stop` |
| Python | `run_python`（dev + safe） |
| Git | `git_status`, `git_diff`, `git_log`, `git_show` |
| 工作状态 | `work_status`, `work_update` |
| 验证 | `validate`（带 working-tree scope 证据） |

## 平台要求

- **Python** ≥ 3.11（使用 `mcp==2.2.0` SDK）
- **操作系统**：Windows（主要测试平台）；核心 runtime 也可在其他平台运行，但 Windows runtime ops（托盘、Credential Manager、PowerShell）是 Windows-only
- **Transport**：STDIO（本地子进程）

## 开发安装

```powershell
# 克隆仓库后
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## 开发启动（源码直跑）

开发和测试可以直接运行源码的 MCP 服务：

```powershell
.\.venv\Scripts\python.exe -m server.main
```

或者安装 editable package 后：

```powershell
localforge
```

**这是开发/测试入口，不是生产 self-use 入口。** 生产使用应通过 release artifact + stable launcher 部署，参见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## MCP Client 接入示例

### Trae（`mcp.json`）

Trae 使用 `mcpServers` 配置。推荐 editable install 后用 venv 绝对路径：

```json
{
  "mcpServers": {
    "localforge": {
      "command": "<repo-root>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "server.main"]
    }
  }
}
```

### OpenCode V2（`opencode.json`）

OpenCode V2 使用 `mcp.servers.<name>` 结构，不同于 Trae 的 `mcpServers`：

```json
{
  "mcp": {
    "servers": {
      "localforge": {
        "type": "local",
        "command": ["<repo-root>\\.venv\\Scripts\\python.exe", "-m", "server.main"],
        "cwd": "<repo-root>"
      }
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "localforge": {
      "command": "<repo-root>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "server.main"]
    }
  }
}
```

### Python SDK（最小直连测试）

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command="python", args=["-m", "server.main"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([t.name for t in tools.tools])


if __name__ == "__main__":
    asyncio.run(main())
```

## Windows Runtime Ops

`localforge_ops` 包提供可选的 Windows 桌面运维能力：

- 托盘 GUI（`localforge_ops.tray`）
- 隧道客户端管理（启动/停止/重启 runtime）
- Windows Credential Manager 集成（存储 tunnel control-plane API key）
- 自启动（可选）

这些能力需要在配置文件或环境变量中提供 tunnel 客户端路径和 tunnel ID。参见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) 的 "Runtime Root Choices" 和 "Security Notes"。

配置文件位置：`%APPDATA%\LocalForge\runtime-ops.json`

## 安全模型简述

LocalForge 设计给**单一可信开发者**在自己的机器上使用。它的安全目标是防止昂贵、高影响的失误，不是把个人开发助手变成对抗性代码执行平台。

- Dev 模式是最宽松的正常开发执行环境——可以写项目文件、跑 shell/PowerShell、用包管理器、继承 `.env`
- Safe 模式为 `run_python` 提供受限分析环境
- 拒绝访问路径：SSH 私钥、浏览器 cookie store、Windows Credential/SAM、tunnel-client 凭据区域
- 输出自动脱敏（API key、token、PEM 块、Authorization header 等）
- 审计日志记录操作元数据但**不记录文件内容、代码参数或环境值**

详见 [docs/SECURITY.md](docs/SECURITY.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -m compileall -q server adapters scripts tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```

## 版本

当前版本：1.1.2（参见 `pyproject.toml`）

## 许可证

**Apache License 2.0**。详见根目录 [LICENSE](LICENSE)。
