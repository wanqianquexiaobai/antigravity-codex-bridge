---
name: codex-delegate
description: 通过 codex-worker MCP 将软件实现、修复、测试或独立代码审查委派给 WSL 中的 Codex。Gemini 保留方案设计、任务调度和验收职责，管理任务进度并根据结果继续迭代。
---

# Gemini 指挥，Codex 实现

使用已连接的 `codex-worker` MCP。工具名可能带客户端生成的前缀，按实际工具列表调用。

1. 首次使用或连接异常时调用 `codex_health`。未登录或服务不可用时说明具体问题，不假装已经委派。
2. Gemini 确定设计意图和验收标准。在 `codex_submit` 的 `task` 中交代目标、相关文件、允许修改的范围、约束、验收方法。上下文和图片不会自动传给 Codex；传入必要文字描述及可读取的文件路径。
3. `working_directory` 使用实际项目目录的绝对路径。支持 WSL 路径、Windows 盘符路径和指向 Debian 的 WSL UNC 路径。不得用用户主目录或应用配置目录作为工作区。
4. 分析、审查使用 `mode="read-only"`；用户授权的实现、修复和会产生文件的测试使用 `mode="workspace-write"`。只在有明确需求时指定 `model`，否则使用执行端默认模型。
5. 每个新任务生成唯一 `request_id`（如 `feature-search-20261113-01`）。同一次提交的重试必须使用相同 ID 和相同参数，避免重复执行。保存返回的 `job_id`。
6. 使用 `codex_status(job_id, wait_seconds=20)` 等待进展。`queued`/`running` 表示尚未完成；不要因此重新提交任务。当前同一时间执行一个任务，遇到忙碌时调用 `codex_list_jobs` 找回已有任务。
7. 进入终态后调用 `codex_result`，有 `next_offset` 时继续读取。`succeeded` 表示代理完成了本轮，不代表所有检查通过；以报告中的实际验证证据验收。把失败、未验证部分和需要用户决定的事项明确说明。
8. 需要修改时调用 `codex_continue`，传入已结束任务的 `job_id`、具体反馈和新的 `request_id`。它返回新的任务 ID，但继续同一 Codex 会话。跟踪新 ID，不使用“最近一次会话”。
9. 用户取消时调用 `codex_cancel`，继续查询直到终态。取消、失败和超时可能留下部分修改，后续先检查现状。连接恢复后使用 `codex_list_jobs` 找回任务；重连不会自动重做任务。

Gemini 负责创意、设计取舍和最终验收；Codex 自主处理实现细节并提供检查结果。Codex 工作期间，不并行修改它正在处理的文件。遇到实质设计变化，由 Gemini 根据证据决定后再下发反馈。

纯构思、解释和无需读写项目的讨论由 Gemini 直接完成。用户明确选择其他工作方式时遵循用户要求。安装这套协作流程不额外授权发布、部署、推送或对外发送信息。
