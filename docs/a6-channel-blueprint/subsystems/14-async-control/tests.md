# S14 Outbox 与 Reconciler：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| ASY-T001 | ASY-001 | `commit_desired_change` | 请求事务提交事实+Outbox后立即返回；HTTP mutation spy 只在 worker。 |
| ASY-T002 | ASY-002 | Outbox uniqueness | 同 resource/generation/kind 重复只一行；新 generation supersede 旧 pending。 |
| ASY-T003 | ASY-003 | `claim_next` | 正确锁序通过；Listing 后再取 credential 被检测/拒绝，无死锁。 |
| ASY-T004 | ASY-004 | lease | 默认30/10；失租 worker 在下一 mutation 前停，接管者可继续。 |
| ASY-T005 | ASY-005 | `run_operation` | 每阶段 append journal；crash 后先读远端并从最后 confirmed phase 继续。 |
| ASY-T006 | ASY-006 | unknown resolution | target/old/third 事实分别 adopt/same-id replay/new-generation replan。 |
| ASY-T007 | ASY-007 | retry/dead-letter | 连续 worker claim/crash 不消耗 retry_round；只有 8 个 RETRYABLE outcomes 才耗尽；REMOTE_REJECTED/NON_RETRYABLE 直接 visible DLQ/review、零 blind retry；人工 replay 重置 retry_round 但保留 identity/journal。 |
| ASY-T008 | ASY-007 | `replay_dead_letter` | current desired 可同 identity 重放；过期 generation 被拒绝/supersede。 |
| ASY-T009 | ASY-008 | event triggers | 所有列出的 generation/参数变化在 commit 后有 Outbox，rollback 无事件。 |
| ASY-T010 | ASY-009 | schedules | fake clock 验证默认频率与错误后 circuit immediate trigger。 |
| ASY-T011 | ASY-010 | shared polling | 100 Listings 同 credential 只发一次完整目录请求并 fan-out；circuit=1000/incomplete 或中间页失败时 confirmed snapshot 不前移、absence/down/clear event spy=0。 |
| ASY-T012 | ASY-011 | concurrency/429 | per credential<=2/global<=10；429只延后 dataset，无 down/cooling。 |
| ASY-T013 | ASY-012 | dedupe identities | attempt/terminal/circuit/route/pricing 重复和乱序均稳定去重。 |
| ASY-T014 | ASY-013 | terminal ownership | 非 aggregator submit 拒绝；重复 stream/callback 只追加 evidence。 |
| ASY-T015 | ASY-014 | observability | queue/lease/phase/retry/completeness 指标齐全，secret leakage scan 为零。 |
| ASY-T016 | ASY-008,ASY-009 | immediate/coalesced events | eligible 正数内变化只投影；跨零、platform-model 变更、安全界和成本上涨立即；Class2 仍用原刷新。 |
| ASY-T017 | ASY-008 | Ban event triggers | 只允许 NEW_PROVIDER_TOKEN、NEW_OWNED_ROUTING_LISTING、SELF_ROUTE_REPAIR；Token/Listing removal 只缩小 membership generation，不产生新的 BAN_INIT_REQUIRED；price/route/普通 relist 同样零 Ban event。 |
| ASY-T018 | ASY-008 | stale ban event | generation 8 的 `BAN_INIT_REQUIRED` pending 期间 generation 移到 9；worker 收到旧 generation-8 事件时不假定初始化完成，而是对照 current generation 9 对账，只补齐 missing relations。 |

## 2. 并发与故障模型

运行多 worker 数据库集成测试：随机 crash、网络 timeout、lease renewal delay、Outbox 重复/乱序、新 generation 穿插。断言每个 remote resource/current generation 最多一个 mutation in flight，最终状态与单线程 reference model 一致。

## 3. 长时间 fake-clock 测试

模拟 24 小时 poll/429/backoff/dead-letter，断言无逐 Listing 请求放大、无固定 blocking sleep、任务不会因一个 credential 429 阻塞其他 credential。

## 4. 验收证据

提供 operation phase journal 示例、worker takeover、unknown readback、DLQ/manual replay、poll fan-out 和并发上限测试输出。生产只读验证 queue/metrics，不为 S14 单独触发业务 mutation。

## 需求覆盖索引

本测试集覆盖：`ASY-001`, `ASY-002`, `ASY-003`, `ASY-004`, `ASY-005`, `ASY-006`, `ASY-007`, `ASY-008`, `ASY-009`, `ASY-010`, `ASY-011`, `ASY-012`, `ASY-013`, `ASY-014`。
