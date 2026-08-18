# S09 Eligibility 与 Delta：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| ELG-T001 | ELG-001 | `evaluate_relationship` | 两个 remote Listing instances 共用 stable binding 时 override/eligibility 历史连续。 |
| ELG-T002 | ELG-002 | evaluator formula | 已知 sk/profit/ck 精确得 required/delta_auto；commission 输入不参与。 |
| ELG-T003 | ELG-003 | `set_delta_override` | null 清空恢复 auto；0/.5 边界有效；负值或>.5拒绝。 |
| ELG-T004 | ELG-003,004 | override hard gate | override 低于所需值时保存但 structural=false，不能绕过利润不等式。 |
| ELG-T005 | ELG-004 | structural matrix | model/native/range/profit 四个条件逐项失效均给稳定 reason。 |
| ELG-T006 | ELG-005,006 | attempt matrix | delta=0 对 incapable profile 可尝试且原 usage；delta>0 必须 profile capable。 |
| ELG-T007 | ELG-007 | `recompute_listing_eligibility` | 每个 required profile 至少一个 candidate 才清 NO_ELIGIBLE。 |
| ELG-T008 | ELG-008 | profile validation | required 空或实际未声明置 config hold；本次账号不被 cooldown/paused。 |
| ELG-T009 | ELG-009 | projection | DB projection + refresh Outbox 同事务；DB commit 后、Redis replace 前 crash，Outbox 可重建同 generation/hash；candidate/requires_delta/capability 一致，满并发不改结构行。 |
| ELG-T010 | ELG-010 | `select_and_freeze_attempt` | candidate 必须来自请求解析出的 exact listing_binding/model/profile；另一 Listing 即使同 model/profile 也不能串入。一个 selection round 对候选 authoritative gate pointer 使用 bounded batch query，禁止 per-candidate N+1 generation loads；只有最终准备 dispatch 的 candidate 做一次 full authoritative fence + snapshot transaction。snapshot 包含列出的 generation、S07 confirmed Listing integer vector/hash 与 delta，attempt_id 唯一；后续 current Listing price 改变不能改变历史 snapshot 的 vector。 |
| ELG-T011 | ELG-011 | pre-send fence | 选中后 route/price/Ban generation 改变则释放并重选；合法 snapshot 只交给既有 native dispatch primitive，S09 无第二套 DISPATCHED/send 状态；已发送 snapshot 不改。 |
| ELG-T012 | ELG-012 | scheduler entry matrix | initial/manual/sticky/slot recheck/failover/retry/Replay 都命中同一 evaluator spy。 |
| ELG-T013 | ELG-013 | `revoke_delta_transport` | schema 失败写 S09-owned exact account/profile revocation + same-tx Outbox；S04 capability mutation spy=0；其他 profile/account/price 保持。 |

## 2. 属性测试

- 对随机正 Decimal 输入断言 eligible 时 `ck*(1+delta)>=sk*(1+profit)`。
- 对相同 composite generation 重算，decision hash 必须稳定。
- 在 select/acquire/freeze/send 四点并发切 generation，只有 send 前全匹配的 attempt 可标 DISPATCHED。

## 3. 集成验收

构造同 Listing 的 Class1/2/3 候选、四种 transport profile 和 delta 0/>0 矩阵，验证数据库 sparse rows、Redis projection、S12 三出口以及 S10 frozen snapshot 输入一致。

## 需求覆盖索引

本测试集覆盖：`ELG-001`, `ELG-002`, `ELG-003`, `ELG-004`, `ELG-005`, `ELG-006`, `ELG-007`, `ELG-008`, `ELG-009`, `ELG-010`, `ELG-011`, `ELG-012`, `ELG-013`, `ELG-014`。
