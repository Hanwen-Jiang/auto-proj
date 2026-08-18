# S01 A6 Typed Connector：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [A6 API Reference](../../../a6-api-reference.md)

## 1. Fixture 原则

使用 API Reference 与 `api-docs/` 的脱敏响应建立 contract fixtures。HTTP mock 必须能注入分页变动、429、超时、响应丢失、重复资源和未知字段；生产 smoke 只在 S16 发布门内执行。

## 2. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| CON-T001 | CON-001 | `call_typed` | credential A 绑定多个资源时每次仍发送同一精确 user/token 对；错 user 被 self readback 拒绝。 |
| CON-T002 | CON-002 | `assert_reference_coverage` | Reference manifest 任一确认端点缺 method 或 mutation readback 时测试失败。 |
| CON-T003 | CON-003 | `typed_decode` | Decimal 字段精确，未知 status/profile/listing 字段被保存并可 round-trip。 |
| CON-T004 | CON-004 | `collect_complete` | 多页正常返回 complete=true，并保存所有页 hash/游标/总数。 |
| CON-T020 | CON-004 | `complete_owned_market_search` | 请求逐字包含 token/model、`route_status=all`、`include_seed=true`、`include_disabled=true`、`include_user_disabled=true`、`exclude_unavailable=false`；漏任一参数或任一页不完整时不得作为 owned exclusion 证明。 |
| CON-T005 | CON-004 | `prove_absent` | 中间页失败、重复游标、页总数矛盾时 complete=false 且不得证明不存在。 |
| CON-T006 | CON-004 | circuit paginator | 恰好 1000 个 circuit 项标记 incomplete，不用缺项证明恢复。 |
| CON-T007 | CON-005 | `execute_mutation` | 正常写入保存 actor/department/generation/idempotency/pre/post hashes。 |
| CON-T008 | CON-006 | `execute_mutation` | mutation 响应丢失但 post=target，返回 CONFIRMED_ADOPT 且不重复写。 |
| CON-T009 | CON-006 | `execute_mutation` | 超时后 post=exact pre，返回 REPLAY_SAME_OPERATION；operation identity 不变。 |
| CON-T010 | CON-006 | `execute_mutation` | post 为第三状态，返回 REMOTE_DIVERGED 且 connector 不自行补偿。 |
| CON-T011 | CON-007 | `read` | 普通 GET 有界重试；`GET /api/user/token` 首次超时后不自动重试。 |
| CON-T012 | CON-007 | `read` | 429 返回 retry hint 并保留 last-confirmed，不生成 down/cooling 字段。 |
| CON-T013 | CON-008 | all HTTP paths | request/response/error/trace fixture 扫描不到完整 Authorization、Cookie、Token 或 Key。 |
| CON-T014 | CON-009 | `admin_console_command` | 白名单读取可执行；mutation 只落 Outbox；非白名单拒绝。 |
| CON-T015 | CON-010 | `update_supplier_profile` | body 只有 contact_info/description；name/rename 输入在发包前失败。 |
| CON-T016 | CON-011 | `delete_token` | detail not-found + 完整列表无 ID 才确认；列表不完整保持 unknown。 |
| CON-T017 | CON-012 | error envelope | Connector 返回错误事实但不会直接 pause account、加 hold 或写 route。 |
| CON-T018 | CON-013 | `update_supplier_channel` | PATCH 含 `api_key/base_url/test_model` 成功；响应丢失时 GET 读回精确判定 target/old/third，完整 Key 不出现在日志。 |
| CON-T019 | CON-014 | `restore_supplier_channel` | restore 空对象 Body 确认；`success:true` 后按 GET readback 复核；超时后 GET 读回三态分类。 |

## 3. Protocol matrix

每个端点至少具备：成功 fixture、业务失败 fixture、非 2xx fixture、未知字段 fixture、redaction 断言。每个 mutation 额外具备正常 readback、响应丢失后 target、响应丢失后 old、第三状态、readback incomplete，以及**服务端明确拒绝且 post 仍为 old 时返回 REMOTE_REJECTED（不得 replay）**的路径。

## 4. 生产前证据

- 使用受控 credential 读取 self/status 和每个只读接口族，记录脱敏 request ID/hash。
- mutation smoke 由所属业务包在隔离资源执行，不由 Connector 测试随意创建/删除生产资源。
- Token delete 证据必须同时包含 detail not-found 和完整列表 absence 的 readback hash。

## 需求覆盖索引

本测试集覆盖：`CON-001`, `CON-002`, `CON-003`, `CON-004`, `CON-005`, `CON-006`, `CON-007`, `CON-008`, `CON-009`, `CON-010`, `CON-011`, `CON-012`, `CON-013`, `CON-014`。
