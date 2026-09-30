# LocalForge Roadmap v2

长期目标：最大程度实现 Codex-like 项目开发能力，并逐步构建一个让 GPT 能操作真实世界的本地 AI Runtime。LocalForge 是 GPT 的本地执行、状态和证据 Runtime，不在内部实现另一个 LLM Agent；public capability 坚持少而强，不为数量堆 MCP tools。

长期运行约束：**Source is for development. Release artifact is for execution.** 源码只负责开发、测试、构建和生产版本；正式 self-use 只运行经过版本化、发布、下载校验和 installed black-box 验证的 release artifact。

## Gate 0 — Foundation

状态：PASS / Closed

已完成并由 GPT 独立 QA 关闭：治理与 task/RESULT handoff、Stable/Dev 边界、MCP SDK v2、16 个 public tools、shell/run_python、Git 与 validate current working-tree scope evidence、基础安全与审计，以及可复核的运行、测试和 Git 证据。

## Gate 1 — Codex-level Coding Runtime

状态：PASS / Closed

总目标：让 GPT 通过 LocalForge 稳定完成真实项目的 Codex-like 开发闭环。

```text
理解项目 → 搜索代码 → 设计修改 → apply_patch → shell 跑测试/构建
→ 分析失败 → 继续修复 → Git diff/status → validate/scope → 交付
```

Milestones：

- M1：完整单任务 coding loop
- M2：失败诊断 → 修复 → 重测
- M3：长任务与后台进程稳定性
- M4：checkpoint / resume / 跨会话恢复
- M5：仅在真实需要时增强 workspace / worktree / code navigation / LSP

checkpoint、session 和 persistent workspace 属于 Gate 1 后半段 milestone，不单独拆成 Gate。优先验证真实项目闭环，不机械复制 Codex 内部设计。

## Gate 2 — Local Automation Runtime

状态：PASS / Closed（Capability Runtime、真实 artifact、hardening、真实 Excel COM closure）

从 Coding Runtime 扩展为 GPT 的通用本地 AI Runtime，方向包括 Python automation、Office/COM、Excel/Word/PowerPoint、PDF、Data、File automation、Windows automation，以及可由 Shell/Python/API/COM/CLI 控制的本地能力。

Office 只是 automation capability 示例，不是产品主线。legacy `office-assistant` 作为 Legacy/Reference 保留。

## Self-Use Deployment Pilot

状态：PASS / Closed

本地源码是开发工作区；Oracle 服务器只保存版本化 release artifact；Windows installed runtime 是日常使用目标。正式闭环为：源码修改 → 测试 → bump version → commit → build → publish → 下载校验 → 独立安装 → installed MCP black-box → 原子切换 current → stable launcher 使用。

## Current Operating Mode — Release-First Self Use

先真实使用，发现问题后回源码修复；每次修复必须版本化发布。日常正式链路不得从源码仓库的 `server.main` 直跑；源码直跑仅限开发和测试。后续新能力根据真实使用问题再立项，不机械新建 Gate。

## Future / Optional

以下不是正式 Gate：多机器、团队/权限、远程执行、自动更新、capability distribution、产品化/安装体验，以及只有真实需求出现后才立项的能力。

Gate 表示不过这一关就不应继续下一大阶段的重大质量门，而不是每增加一类功能就新建一个 Gate。
