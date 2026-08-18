# S12 调度、错误与 Incident：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [错误分类合同](../../../../errorlist.md)

## 1. 三出口与分类

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| ERR-T001 | ERR-001 | `schedule_request` | initial/manual/sticky/recheck/failover/retry/Replay 都调用 S09 gate。 |
| ERR-T002 | ERR-002 | three exits | exact Listing/profile 的 eligible=0、dispatched-all-fail、all-busy 三者互斥；初选后 generation churn 导致零 dispatched 但仍有 eligible 时只 retry selection，不制造 aggregate cooldown。 |
| ERR-T003 | ERR-003 | `classify_attempt` | 用户输入/auth/policy/model 等确定性错误在 SSE guard/timer 创建前短路；client cancel 立即取消 timer/upstream，账号/route/Listing 无处罚、零 synthetic prelude。 |
| ERR-T004 | ERR-004 | context split | user invalid key 与 Provider invalid key 分入 USER/ACCOUNT；quota 交 liquidity。 |
| ERR-T005 | ERR-005 | route split | Auto-Pool error 发 expand，Pin error 发 repin，二者不改 mode。 |
| ERR-T006 | ERR-006 | unknown failures | 单次裸 timeout/5xx/unknown 默认 cooldown trigger，不错误归因 Provider。 |
| ERR-T007 | ERR-007 | side-effect scope | 单 attempt 只影响 exact account/route；`record_attempt_failure_once` 按 attempt_id 幂等。构造 attempt A 上游失败→failover→attempt B 成功，A 的窄作用域 owner action 仍恰好一次，且不产生 Listing hold。 |
| ERR-T008 | ERR-008 | aggregation order / async finalize | 所有 failed attempt 先各自通过短事务幂等 `record_attempt_failure_once`；随后 request aggregator 的独立短事务只写 eligibility-recompute + 带 immutable aggregate 摘要的 `FINALIZE_AGGREGATE` 并立即返回失败。模拟 S09/Outbox 堵塞时 request goroutine 不等待；断言不存在嵌套事务、remote I/O 跨事务或第二套 request-terminal 表。finalizer 在 recompute generation 就绪前只异步重试；就绪后 eligibility=0 仅 NO_ELIGIBLE，>0 才允许 local cooldown/incident。重复 finalizer 零重复 side effect。 |
| ERR-T009 | ERR-009 | explicit incident | 权威 global/DB/Redis health failure 直接建正确 scope incident。 |
| ERR-T010 | ERR-010 | promotion threshold | 同 request 两 identity 或 60s 两 Listing 提升；单独一次不提升。 |
| ERR-T011 | ERR-011 | `failure_fingerprint` | 所有规定字段影响 hash，member ID 不影响 hash。 |
| ERR-T012 | ERR-012 | membership | local member 有 owner hold；REMOTE_COOLING 只有 membership/observed，无伪 hold。 |
| ERR-T013 | ERR-013 | `recover_incident` | canary 成功才批量；<=10、5-15s；首失败停止；过期 witness 拒绝。 |
| ERR-T014 | ERR-014 | SSE prelude / pre-commit failover | 参数化覆盖：deadline 始终在 500..2000ms；deadline 前上游 event 到达则零 prelude；deadline 先到则只写一次 `:\n\n` 并 flush，后续 upstream event 原连接继续。prelude 后 candidate-local/upstream-attributable error 在 event 未 committed 时必须经 S09 gate 切下一未尝试账号且不重置 timer/不发第二 prelude；USER/platform/Listing/local/unknown 不走该特殊 failover。第一段 upstream event 一旦转发，后续任何错误都零第二 upstream。10k 并发 timer/prelude 模拟断言每请求最多一个 timer/flush、无 ticker、**guard/prelude 自身零 DB write**、无 durable guard、取消后无 timer leak；失败 attempt 仅保留 ERR-007 已有窄作用域写。 |

## 2. 错误表覆盖

把 `errorlist.md` 中每个 canonical code 和上下文分流表变成参数化 fixture。每个 fixture 断言 classification、attempt side effect、aggregate side effect、**SSE pre-commit request disposition（TRY_NEXT / existing handler / short-circuit）** 和禁止动作；SSE 层不得复制一份错误码表。尤其覆盖 `rate_limit_exceeded`、`model_not_available`、`missing_terminal_event`、`unauthorized` 的多上下文。

## 3. 去重与竞态

- 重复 attempt callback 只发送一次 account/route command。
- 两 aggregator 竞争同 request，只有唯一 terminal event owner 可提交 Listing 事件。
- incident promotion 时成员同时恢复/退役，只处理 matching incident generation 的 active member。

## 4. 验收证据

保存分类矩阵、三出口计数、incident fingerprint fields、canary/batch journal。证据中 endpoint/region 可保留，credential 只保留 scope/fingerprint。

## 需求覆盖索引

本测试集覆盖：`ERR-001`, `ERR-002`, `ERR-003`, `ERR-004`, `ERR-005`, `ERR-006`, `ERR-007`, `ERR-008`, `ERR-009`, `ERR-010`, `ERR-011`, `ERR-012`, `ERR-013`, `ERR-014`。
