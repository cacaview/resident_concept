# First prompt for Claude Code

请先完整阅读 VISION.md、ARCHITECTURE.md、GENOME.md、EVENT_MODEL.md、DEVELOPMENT.md、CLAUDE.md。

这是一个“数字生命体 / persistent digital individual”的概念项目，而不是传统 Goal/Task Agent。

直接开始第一轮开发：

1. 检查并修复现有 skeleton，使 backend / frontend / tests 都可以实际运行。
2. 完整实现 EventStore 的最小可靠版本，包括 append、get、list、since 和基础错误处理。
3. 完成 WebUI 的 Chat + Life Timeline。
4. 实现可测试 MindLoop：支持 continuity / revisit / distant / serendipity / self / rest，必须允许 wake.noop，不得用 next_goal() 或 autonomous task queue 作为核心。
5. 增加最小 Memory Retrieval 接口，先支持最近事件/关键词检索，embedding 留 provider 接口。
6. 加入 Thread / Thought / Question 的基础持久结构。
7. 实现 Re-entry 第一版数据管线，只从真实事件生成候选，不允许编造“离屏经历”。
8. 写测试证明：重启后事件仍存在；no-op wake 合法；thought 可链接旧事件；re-entry 不会引用不存在的活动；不同 cognitive route 会被显式记录。
9. 不要自行扩展到邮件、生产服务器控制、复杂多 Agent、自我修改、情绪数值系统。
10. 对仍不确定的设计写 ADR/TODO，不要为了方便把架构改成 Goal → Planner → Task → Done。

完成后：跑测试，给出运行方法，汇报 Proof-of-Life 链路打通到哪里，并列出下一阶段最值得解决的 3 个问题。
