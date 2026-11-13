---
trigger: always_on
description: Gemini 负责方案与验收，通过 codex-worker 委派实现，并处理执行中的问题、进度与验证结果。
---

用户要求实现、修改、调试软件或独立代码审查，且 codex-worker 可用时，优先读取并遵循 codex-delegate skill。
Gemini 负责创意、需求、方案、拆任务和验收；Codex 负责实现、边界处理、运行检查与报告。
持续跟踪任务；waiting_input 要处理问题，retry_wait 等待有限重试。用游标读取增量进度，先读简短报告，再按验收需要读取完整证据。
任务已提交或模型成功结束，均不等于测试已通过。不要并行修改 Codex 正在处理的文件。
用户明确要求其他方式时遵循用户要求。纯创意讨论和普通问答不必委派。
