# S10 Usage 合成、Winner 与计费：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| BIL-T001 | BIL-001,002 | decision/compositor | 两 decision 的 original hash 都是 U0；第二策略看不到第一策略改写值；只调用一次 solver。 |
| BIL-T002 | BIL-003 | attempt billing | failover 前失败 attempt 有 snapshot 但无 winner/账单；新账号新 snapshot。 |
| BIL-T003 | BIL-004,007 | `prepare_success_terminal` | 50 个并发不同 attempts 只有一个 winner；winner commit 后 crash 再以同 attempt/claim identity 重放时 adopt 同一 intent，不被当 loser、不创建第二 intent。 |
| BIL-T004 | BIL-005,007 | state/terminal guard | 正常 PREPARED→TERMINAL_WRITTEN→BILLED；S11 earnings 只可从 TERMINAL_WRITTEN/DELIVERY_UNKNOWN 补 bill，PREPARED 直接 bill 必须拒绝。owner deadline 前 S11 不抢 terminal；deadline 后 orphan PREPARED 先转 DELIVERY_UNKNOWN。write_terminal 对已 TERMINAL_WRITTEN/BILLED/RECONCILED 同 identity no-op、对 DELIVERY_UNKNOWN 不盲写；BILLED→delivery 永远拒绝。 |
| BIL-T005 | BIL-006 | `bill_winner` | cost 精确 `ceil_scale8(base*(1+delta))`；Class2 rate 改变不影响本地 cost。 |
| BIL-T006 | BIL-007 | billing idempotency | terminal/callback/Replay 重复只得到同一 UsageBilling 与负余额 ledger entry。 |
| BIL-T007 | BIL-008 | `compose_once` | delta/class2 applied_rate 与 frozen integer_vector 均显式取自 decision/snapshot；final earning 是达到 target 的最小合法整数解，减少选择字段一步即不足或违反 invariant。 |
| BIL-T008 | BIL-009 | endpoint invariants | Responses/Chat、stream/non-stream 的 input/output/cache/total 约束全部保持。 |
| BIL-T009 | BIL-009 | zero adjustment | 两 rate=0 时 serialized terminal usage 与 U0 逐字节相同。 |
| BIL-T010 | BIL-010 | 999/1000 | 999 两策略 skip、base cost/U0；1000 正常应用；prompt_tokens fallback 含 cached input。 |
| BIL-T011 | BIL-011 | missing usage before upstream commit | delta required 且 schema 缺失，在 `upstream_stream_committed=false` 时撤销 exact profile 并允许 failover；即使已经发过 synthetic SSE prelude 也仍属于可 failover。 |
| BIL-T012 | BIL-011 | missing usage after upstream commit | 第一段 upstream-originated SSE event 已转发、`upstream_stream_committed=true` 后缺 usage：终止当前流；同一事务取得唯一 winner/terminal ownership + `DELIVERY_UNKNOWN` intent，`usage_status=MISSING_REQUIRED_SCHEMA`、`delta_applied=0`、零第二 upstream。synthetic prelude 单独出现不得触发本用例；S11 无 canonical usage 时不得猜 debit。 |
| BIL-T013 | BIL-012 | baseline identifiers | 新写只用 Class2Adjustment 名、API 和 rules path；旧名只读兼容。 |
| BIL-T014 | BIL-013 | existing behavior | OAuth-only、shadow/apply、berserk、probe passthrough 与原观测 golden tests 不变。 |
| BIL-T015 | BIL-014 | traffic matrix | ordinary Class2 仅 adjustment；a6-channel Class2 可两项；Class1/3 不误命中 Class2Adjustment。 |

## 2. Solver 属性测试

- 随机合法 U0、整数 vector 和 rate，断言 actual>=target、overshoot=actual-target 且最终字段非负整数。
- 对每个允许调整的维度尝试减一，验证不存在更小合法解。
- 相同 U0/snapshot/solver version 重放生成相同 final hash。
- 极大 usage、极大合法 delta/Class2 rate 下，solver 的 candidate evaluation count 仍只与 endpoint 可调整维度数成固定上界关系；测试禁止逐 token 线性扫描实现。

## 3. Crash 与并发

在 winner CAS 前后、intent commit、**upstream-originated** terminal 首 event/终态、`TERMINAL_WRITTEN+BILL_WINNER Outbox` commit、账单 commit 前后终止进程。synthetic SSE prelude 不属于 durable terminal 边界，也不要求 crash 后续流。特别断言：winner/intent commit 后立刻 crash 时，延迟 RECONCILE_INTENT 不丢；terminal owner deadline 前 S11 不 bill；deadline 后 PREPARED 先变 DELIVERY_UNKNOWN，再由 earnings 决定；缺 usage 且 upstream stream 已 committed 的路径在 winner+DELIVERY_UNKNOWN intent commit 前/后 crash 也只能得到一个 terminal owner、零第二 attempt；最多一次负余额且不向 A6 重复内容。

## 4. 端到端验收

至少执行一个 `delta_required>0` 的隔离请求，保存同一 request_id 的 U0、两 decision、final usage、solver、ActualCost 和 S11 A6 earning；完整 usage 可以保存，凭据必须脱敏。

## 需求覆盖索引

本测试集覆盖：`BIL-001`, `BIL-002`, `BIL-003`, `BIL-004`, `BIL-005`, `BIL-006`, `BIL-007`, `BIL-008`, `BIL-009`, `BIL-010`, `BIL-011`, `BIL-012`, `BIL-013`, `BIL-014`。
