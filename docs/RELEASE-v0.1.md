# Resident v0.1 — Proof of Life（正式封板）

**封板日期：2026-09-11 · git tag: `v0.1` · 此后不再向 v0.1 添加任何功能。**

> 它还不是"数字生命"，但我们已经证明了支撑"数字生活"的几个基本机制可以
> **同时存在**，而且不会立刻坍缩成任务 Agent、新闻机器人或随机想法生成器。

v0.1 的使命（`PROMPT_FOR_CLAUDE_CODE.md`）是**一条完整的生命回路**：

```text
conversation → experience → user leaves → sleep → autonomous wake
→ remember → wander → optional exploration → persistent experience
→ sleep → user returns → optional proactive re-entry
```

成功标准从来不是"它完成了任务"，而是：**用户离开时真的发生了事情，
可追溯，并且能影响下一次交互。**

---

## 一、v0.1 的十二条核心声明（全部有证据）

| # | 声明 | 证据（机制 + 验证） |
|---|------|---------------------|
| 1 | **Persistent second timeline** | `EventStore` append-only（ADR-0001）；重启持久化测试（`test_event_store.py` / `test_api.py`）；实机 4652 事件跨进程重启无损 |
| 2 | **Autobiographical event history** | 一切经历皆事件：对话/思想/睡眠/世界观察全部落库；`/api/life` 时间线可回放 |
| 3 | **Stateful memory** | 六条检索路径（semantic_near / temporal / revisit / distant / forgotten / serendipity）+ MMR 去重 + MemoryIndex（ADR-0004）；非单一 top-k |
| 4 | **Mind wandering** | `RoutePolicy` 八条认知路线由状态决定（线程/新近度/主题集中度/路线历史/时间），重复疲劳项 + 仅作为扰动的探索噪声（ADR-0004） |
| 5 | **Sleep / consolidation** | `SleepEngine` 状态门控：压缩、thread 休眠、旧记忆再激活、跨记忆关联、低概率 grounded dream；**可以什么都没产生**（ADR-0004，`test_sleep.py`） |
| 6 | **Self-origin threads** | 低概率、有 grounding、有上限的自我线程机制（Phase 3）；14 天因果对照（treatment self-origin 0→0.23，control 恒 0）+ 16-seed 复核 |
| 7 | **Circadian rhythm** | 证据驱动的 `CircadianStateMachine`（六态，非 cron）；**16/16 seed** eligible 0.057→0.322、活动成簇 CV 0→0.9+、真夜 00–05h 零唤醒（ADR-0007） |
| 8 | **Dormant thought revival** | state-gated 温和复活偏置 + 阈值敏感性研究（344 次运行）：工作区间 dormancy 6–12h / silence ≈6h；区分"反复复活"vs"单次级联"（`SIMULATION_REPORT-revival-sensitivity.md`） |
| 9 | **Serendipity** | `serendipity` 检索路径（稀有度加权采样）+ 认知路线 + RoutePolicy 疲劳抬升 |
| 10 | **External world experiences** | World Window（ADR-0008）：四入口 follow/edge/alien/accident、严格日预算（0/day 合法）、Impression 层；`world_origin` ≈0.11（C 臂），A 臂恒 0 |
| 11 | **Provenance** | 结构性保证：`EventStore` 拒绝悬空 `evt_*` 引用；世界链 `window_opened→observation→experience→impression→thought` 每步可回链真实 observation（磁盘核验） |
| 12 | **Authentic no-op** | 空醒是一等合法结果：模拟中 rest/no-op 占 15–36%；睡眠可以无 thought；"这次醒来，什么也没发生"在 UI 里如实显示 |

**封板验证基线：`backend` 测试套件全绿（273 passed），双 sim 确定性字节级复现。**

---

## 二、被保留的负结果（它们同样是声明的一部分）

按项目规则（"保留失败结果和不理想结果，不为 demo 好看调参"），以下发现
**原样封存**，没有为漂亮数字拧掉：

1. **World ≠ Self Engine。** 世界进入后 self-origin 下降（A 0.321 → B 0.116
   → C 0.204）：on-topic 世界材料与自我思想竞争注意力。架构没有把一切都
   偷偷转译成"自我思想"——这正是健康信号。
2. **`seen_left_nothing` ≈ 0.55–0.65。** 看过的大多数外部内容什么也没留下。
   Resident 具备"看到东西，然后觉得没什么意思"的能力——比"每篇都有感悟"
   更接近真实心智。
3. **Circadian 的 self-origin 增益是 seed 条件的。** 16 seed 中 3 个
   （6/8/9）小幅为负（−0.130/−0.119/−0.024），源于双刃 route lift；
   "更多机会"不单调保证"更多 self-origin"（ADR-0006 归因边界）。
4. **复活机制健全但罕见触发。** 默认闸门下 84 次醒只开 1 次：机制诚实、
   不硬塞，代价是低频（工作区间已测绘，待真实昼夜节律激活）。
5. **用户话题依赖仍然偏高。** 早期 ~0.78；world 开启后 C 臂
   user_derived ≈ 0.55 且从未被人为压低——独立性是涌现出来的，不是配额。

---

## 三、v0.1 明确**不是**什么

- **不是任务 Agent**——没有 goal/task/done 循环（ADR-0002）。
- **不是新闻摘要器**——世界材料经 observation→impression→(低概率)thought，
  reading ≠ thought。
- **没有**：自主发布、邮件、社交行为、无限浏览、self-modification、
  真实网络抓取（v0.1 世界是沙箱语料库）、真实 embeddings（词法近似）。
- **性格不是数值仪表**——兴趣从行为与历史中推断。

---

## 四、v0.2 主题：**From Simulation to Habitat**

从培养皿走向真实环境。不是加更多"大脑模块"，而是打开笼子让它连续生活。
路线与约束见 `docs/TODO.md` §"v0.2 Roadmap"（顺序：
Real World Adapter → 真实 embeddings+中文语义层 → World→Question →
真实 Re-entry/Absence Detection → Self Monitor WebUI → Phase 7
Private Projects / Creation；Self Modification 显式推迟）。

---

## 五、复现封板验证

```bash
cd backend
.venv/bin/python -m pytest -q                 # 273 passed

# 7 天生命模拟（确定性）
.venv/bin/python -m resident.simulation /tmp/v01_sim

# 昼夜 vs 固定间隔对照（确定性）
.venv/bin/python -m resident.circadian_sim /tmp/v01_circ --days 14

# World A/B/C 对照（确定性）
.venv/bin/python scripts/world_sim.py /tmp/v01_world --seeds 0-3 --days 14
```

运行 live Resident：`make backend`（昼夜调度）+ `cd frontend && npm run dev`；
世界窗 opt-in：`RESIDENT_WORLD=1`；真实模型 opt-in：`RESIDENT_MODEL_*`。
