# S13 Listing 状态与恢复：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| REC-T001 | REC-001,002 | hold API | 每种 hold 保存 owner/source/generation；错误 owner 或旧 witness 不能清。 |
| REC-T002 | REC-003 | `project_desired` | parent inactive 或任一 hold -> UNLISTED；全清且 active -> LISTED。 |
| REC-T003 | REC-004 | `project_admission` | parent maintenance/admission、market/circuit completeness、visible/available/circuit/generation 任一缺失即 CLOSED；incomplete snapshot 不能用 absence OPEN。 |
| REC-T004 | REC-005 | display mapping | local hold/remote unlisted/remote cooling/unknown 显示和计划严格对应。 |
| REC-T005 | REC-006 | `observe_listing` | 普通 GET 失败只 stale；mutation unknown 或完整矛盾才 UNKNOWN。 |
| REC-T006 | REC-007,015 | `choose_plan` | 用组合表穷举优先级；父 Channel maintenance 或 BAN override 存在时普通 child planner mutation=0；否则 current operation 永远优先。 |
| REC-T007 | REC-008 | writer ownership | Listing-scope takedown/resubmit/reset 只有 S13 Output Recovery 可写；Channel-scope takedown/restore/maintenance 只有 S03；Desired/Observer/Planner 发起直接 mutation 被拒。 |
| REC-T008 | REC-009 | local cooldown | 15s/120s/10m 边界；到期只 readiness 检查，HTTP data-plane spy 零调用。 |
| REC-T009 | REC-010 | `direct_relist` | Supplier readback 成功但 marketplace 不可见时仍 CLOSED；完整 witness 后 OPEN。 |
| REC-T010 | REC-011 | `cooling_epoch` | 相同字段同 hash 且只 reset 一次；任一规范字段改变为新 epoch。 |
| REC-T011 | REC-012 | `cooling_reset` | 固定 takedown/wait/resubmit/verify 顺序；中途 UNLISTED 不改成 direct relist。 |
| REC-T012 | REC-013 | recurrence | 5m 内复发120s，再复发10m cap并告警；仍可重放。 |
| REC-T013 | REC-014 | generation change | verification 前 generation 被替换时不得 OPEN，旧 op supersede/replan。 |
| REC-T014 | REC-008 | scope | channel scope 一个 parent mutation journal；model scope 逐 Listing。 |
| REC-T015 | REC-015 | BAN maintenance crash | desired=UNLISTED → 持久化 pre=UNLISTED + durable override 后才 temporary resubmit；在 override commit/POST/readback 任一点 crash，replay 不重采样 LISTED，Marketplace poll=0、ordinary planner mutation=0、admission CLOSED；exact restore UNLISTED 后才清 override。 |
| REC-T016 | REC-015 | caller gate | 非 S05 BAN_INIT caller（如普通恢复/改价）请求 maintenance_visibility_override 被拒绝；`OUTPUT_KEY_ROTATION` hold 不存在于 hold 集合。 |

## 2. 状态空间测试

生成所有 hold presence、desired、observed、current operation、readiness 组合，断言 planner deterministic、admission 保守、无不可达/双动作状态。用 model checker 或 property test 验证一个 generation 下最多一个 remote recovery operation。

## 3. 故障注入

在 takedown 发包后丢响应、wait 时 lease loss、resubmit 后 GET 失败、marketplace visible 但 circuit blocked、verify 后 commit 前 crash。恢复后从 journal/remote fact 继续且不重复同 epoch reset。

## 4. 生产验收证据

对隔离 Listing 执行 direct relist 与 cooling reset 各一次，保存 Supplier mutation audit、Marketplace exact match、circuit clear、generation witness 和最终 admission；不发送额外数据面 Probe。

## 需求覆盖索引

本测试集覆盖：`REC-001`, `REC-002`, `REC-003`, `REC-004`, `REC-005`, `REC-006`, `REC-007`, `REC-008`, `REC-009`, `REC-010`, `REC-011`, `REC-012`, `REC-013`, `REC-014`, `REC-015`。
