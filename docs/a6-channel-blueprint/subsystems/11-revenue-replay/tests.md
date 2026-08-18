# S11 收益对账与 Replay：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| REV-T001 | REV-001 | `reconcile_intent` | 一个 request_id 对齐 attempt、U0/final、UsageLog、billing、ledger、vector 和 earning。 |
| REV-T002 | REV-002 | unique reconciliation | 重复 earnings/callback/Replay 只一行 reconciliation/账单；不同 usage hash 冲突拒绝。 |
| REV-T003 | REV-003 | ratio checks | 三个比例分别计算，delta 只影响 local ratio，公开 quota ratio 目标 1.2。 |
| REV-T004 | REV-004 | tolerance | 1 micros、solver overshoot、price quantization 三者取最大；包络内 deviation=0。 |
| REV-T005 | REV-001,005 | delivery unknown | PREPARED 且 terminal owner 仍有效时只等待；owner deadline 失效先原子转 DELIVERY_UNKNOWN。随后 earnings 找到才完成唯一账单/对账。BIL-011 missing-usage intent：有 authoritative canonical usage 时仅按 resolved base cost、`delta_applied=0` 幂等 bill；无 canonical usage 时 visible reconciliation dead-letter、保持 DELIVERY_UNKNOWN、零猜测 debit。 |
| REV-T006 | REV-005,011 | void gate | earnings+settlement 都 complete/absent 且过终局窗口时，仅 TERMINAL_WRITTEN/DELIVERY_UNKNOWN → VOID；PREPARED 必须先经过 owner-expired → DELIVERY_UNKNOWN，BILLED 保持 BILLED 并进 late sweep；任一不完整不得 VOID。 |
| REV-T007 | REV-006 | late sweep | sweep 同时覆盖 VOID tombstone 与 BILLED-unreconciled；VOID 迟到只重开原 intent 并一次 adjustment，BILLED 迟到只 anomaly+reconcile Outbox、零新 debit；重复 sweep 幂等。 |
| REV-T008 | REV-006 | billed latency | 已 BILLED 的迟到记录只增加 latency anomaly，不新增 ledger。 |
| REV-T009 | REV-007 | `next_earnings_poll` | 0-10m 每15s、10m-24h每5m、之后 daily，边界精确且任务去重。 |
| REV-T010 | REV-008 | scope | shared vector -> Listing hold；exact schema -> account/profile；Class2-only 不扩散。 |
| REV-T011 | REV-009 | threshold | 15m 三个不同 request 触发；同 request Replay 不计多个；单次>5%立即触发。 |
| REV-T012 | REV-010 | `replay_request` | redelivery 复用 final hash/identity；只有 winner absent 且无 downstream commitment 才允许新 attempt 走 S09；winner 已存在时 upstream-attempt spy=0。 |
| REV-T013 | REV-012 | immutable journal | 后续状态只追加 observation/adjustment，不覆盖原 usage、billing 或 tombstone。 |

## 2. 时序与故障注入

模拟 earnings 在 1 分钟、9:59、10:00、23:59、24 小时后、VOID 后到达；在查询分页中断、账单 commit、状态 CAS、late adjustment commit 各点 crash。重启后总 debit 与唯一 request identity 保持一致。

## 3. 端到端验收

使用 S10 的 `delta_required>0` 请求，确认 A6 `earned_micros` 可由 final usage 和冻结 vector 重算，local/base ratio 对应 delta，public/supplier ratio约 1.2。报告列出容差包络和偏差，不泄露 credential。

## 需求覆盖索引

本测试集覆盖：`REV-001`, `REV-002`, `REV-003`, `REV-004`, `REV-005`, `REV-006`, `REV-007`, `REV-008`, `REV-009`, `REV-010`, `REV-011`, `REV-012`。
