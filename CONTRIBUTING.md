# Contributing

欢迎贡献 LocalForge 的代码、文档、问题报告和想法。

## 开发环境

```powershell
git clone <your-fork-url>
cd localforge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## 测试

提交前请确认通过：

```powershell
.\.venv\Scripts\python.exe -m compileall -q server adapters scripts tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```

## 代码风格

- Python >= 3.11，使用现代语法
- 类型注解优先（`from __future__ import annotations`）
- 不引入无必要依赖
- 保持现有 16 个 MCP 工具的 public surface 稳定（除非经过充分讨论）
- `shell` 和 `run_python` 是核心原语——所有新 capability 都应基于它们构建，而非直接向 MCP server 添加新 tool

## 提交规范

- 一个 commit 一件事
- commit message 使用 Conventional Commits 格式（`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`）
- 不要把本地 `.env`、凭据、私钥、运行日志提交进来

## Security Issues

**请不要在公开 issue 中报告安全漏洞。**

LocalForge 目前由 solo maintainer 维护，暂无公开安全邮箱。请使用 GitHub Security Advisory 的 "Report a vulnerability" 功能进行私人报告。我们会尽快响应。

所有凭据、API key、token 都应只存在于本机配置文件、环境变量或 Windows Credential Manager 中，绝不进入 Git 历史。
