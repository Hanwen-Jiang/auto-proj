# S15 管理端与 Read Model：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| ADM-T001 | ADM-001 | `list_admin_users` | a6-channel 精确过滤；role 相同的普通用户不混入；旧 filter 回归。 |
| ADM-T002 | ADM-002 | row serializer | Channel/Listing/eligible/status/负余额/freshness/Key last4 完整，secret 不出现。 |
| ADM-T003 | ADM-003 | `get_a6_channel_drawer` | 每 Listing/账号/transport 与 generation/hold 四维状态可解释，查询无 N+1 remote。 |
| ADM-T004 | ADM-004 | RBAC | input operator 不能轮换 ingress Key，output operator 不能改 Provider route；superuser 审计部门仍明确。 |
| ADM-T005 | ADM-005 | `preview/submit_policy_change` | 字段 set/clear/Decimal 边界正确；提交只产生 typed owner command，S04/S06/S08/S09 current table direct-write spy=0。 |
| ADM-T006 | ADM-006 | corpus UI/API | 只有选择/随机、无 free-text；selection commit 与 owning reconcile Outbox 同事务，commit 后通知丢失仍可由 Outbox 执行。 |
| ADM-T007 | ADM-007 | preview parity | 预览结果与提交后 S08/S09 重算逐项一致，generation 变化时提交 409。 |
| ADM-T008 | ADM-008 | operational views | unknown/exhausted/orphan/DLQ/incident/review/hold 均显示 owner、source、允许动作。 |
| ADM-T009 | ADM-009 | `replay_operation` | same identity/current generation 可重放；旧 generation 拒绝；observed 不变。 |
| ADM-T010 | ADM-010 | `resolve_binding` | 只接受三个动作和确切 readback；任意 ID/直接状态编辑拒绝。 |
| ADM-T011 | ADM-011 | `a6_console_request` | read whitelist 执行；managed write 仅 Outbox；任意路径/方法拒绝。 |
| ADM-T012 | ADM-012 | security | CSRF、reauth、RBAC、audit 和 API snapshot secret scanner 全通过。 |
| ADM-T013 | ADM-014 | risk profile one-way | risk_score 变化（delta 指标/收益异常/平台警告）不产生任何 pricing generation 或 rank 变更。 |
| ADM-T014 | ADM-013 | price rank command | admin 提交 rank command → S15 不直接 mutation price_order 表（spy=0）→ 恰好一个 S08 owning operation（`apply_merchant_price_order_command`）→ S08 写新 generation → 恰好一次 pricing replan；非管理员/过期 generation 拒绝。 |

## 2. 前端交互与响应式测试

使用真实最长模型名、多个 hold、超长 reason/generation 和负余额，检查桌面与窄屏无重叠/截断。操作提交后显示 operation pending/readback，而不是立即“成功上架”。键盘导航、焦点、表格/抽屉可访问名称必须通过现有前端测试基线。

## 3. 性能与一致性

- 100 个 Channel、每个多 Listing 的列表/抽屉查询使用限定批量查询数。
- DB 与 Redis generation 不同显示 lag；remote readback stale 不渲染为 ACTIVE。
- 并发两个管理员按同 expected generation 提交，只有一个成功。

## 4. 验收证据

保存权限矩阵、API contract、read-model query profile、secret scan 和关键页面截图；截图不得展示完整 Key/Token。

## 需求覆盖索引

本测试集覆盖：`ADM-001`, `ADM-002`, `ADM-003`, `ADM-004`, `ADM-005`, `ADM-006`, `ADM-007`, `ADM-008`, `ADM-009`, `ADM-010`, `ADM-011`, `ADM-012`, `ADM-013`, `ADM-014`。
