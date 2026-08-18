# S00 基础数据、安全与公共文本：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 测试层级

- 单元：Decimal、前缀、redactor、corpus parser 和平台长度投影。
- 数据库：约束、current pointer CAS、稳定 binding/instance、migration 重入。
- 安全：secret envelope、授权、内存/输出通道泄漏扫描。
- 集成：语料选择到业务写命令硬门，不执行真实 A6 mutation。

## 2. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| FND-T001 | FND-003 | `decimal_from_wire` | 给定 `0.1` 字符串可精确参与比较；给定 binary float 经济输入则拒绝。 |
| FND-T002 | FND-003 | `ceil_scale`, `micros_ceil` | 覆盖正数、零、边界和高精度尾数，结果始终按指定 scale 向上。 |
| FND-T003 | FND-010 | `normalize_sk` | 无前缀、一个或多个前缀最终都得到且仅得到一个 `sk-`；空材料拒绝。 |
| FND-T004 | FND-001 | `replace_remote_instance` | 首次 adopt 在无 current instance 时创建 generation 1；replacement 关闭旧 instance、创建新 generation；同 remote ID/readback 重放 no-op，不额外前移 generation；历史 attempt 外键仍指向旧稳定 binding。 |
| FND-T005 | FND-002 | migration constraints | 并发创建第二 active credential/current remote ID/effective Key 被数据库拒绝。 |
| FND-T006 | FND-011 | generation CAS/replay | 两个并发 writer 只有一个 CAS 成功，旧 generation 内容不变；operation-driven generation 在 row+current commit 后、phase journal 确认前 crash，same `change_identity` replay 必须 ADOPT 同一 row；更晚 current 已存在时返回 SUPERSEDED、绝不回退。 |
| FND-T007 | FND-004 | `store_secret` | 数据库无明文，解密可还原，AAD 换 credential 后认证失败。 |
| FND-T008 | FND-005 | `redact` | header、嵌套 JSON、error、trace、panic fixture 中完整 secret 全部消失，last4/fingerprint 保留。 |
| FND-T009 | FND-006 | `load_corpus_catalog` | 精确 active 路径的 xlsx 两个 sheet 和 JSON corpus 可读，SHA-256 与保存值一致；归档副本拒绝。 |
| FND-T010 | FND-006,FND-009 | `select_public_text` | Supplier/Channel 各取同一行规定字段；nickname 只截取 name；Listing display_name 为空。 |
| FND-T011 | FND-007 | `select_public_text` | 相同 selection generation 重试得到同一行/索引，不再次随机。 |
| FND-T012 | FND-007 | `handle_remote_name_conflict` | 唯一性冲突选择另一未用项，文本无 ID、模型、hash、计数或后缀；replacement selection 与 owning follow-up Outbox 同事务，commit 后 crash/replay 仍只得到同一 replacement generation 并继续 reconcile。 |
| FND-T013 | FND-008 | `load/select` | 文件损坏、字段缺失、耗尽分别设置 `TEXT_CORPUS_INVALID` 并拒绝远端命令。 |
| FND-T014 | FND-008 | corpus gate integration | 输出失败投影 Listing hold；输入 Token 失败使账号不可调度，二者不串 owner。 |
| FND-T015 | FND-012 | `migrate_foundation` | 空库、已有合法数据、重复运行均成功；冲突数据输出稳定清单并零静默修复。 |
| FND-T016 | FND-008 | legacy Supplier gate | 既有 Supplier 历史 name 不在语料：保存为 `LEGACY_REMOTE`、允许 ACTIVE、不产生 `TEXT_CORPUS_INVALID`，且不能被当作未来新写入来源。 |
| FND-T017 | FND-013 | schema/price order | `a6_merchant_price_orders` active rank 唯一、每商户至多一个 current rank；BanRelation 唯一键 `(token_instance, listing_instance)` 且 state ∈ PENDING/READY/FAILED；Risk Profile 只作为派生 read model 存在。 |
| FND-T018 | FND-014 | membership increment | Token/Listing enter 与“远端删除精确确认 + 完整 current Supplier absence”退出的 membership DML、`ban_membership_generation` +1、audit 同事务；各 commit-boundary crash 整体 rollback，commit 后 replay 不重复 +1；本地 retire/takedown/replacement 退出被拒绝。 |
| FND-T019 | FND-014 | membership stability regression | 连续 100 次 price/route/list/unlist/cooling/max_ratio/EXCLUSION_GATE readback → `ban_membership_generation` 不变；只有真实 member enter/confirmed remote-delete exit 可递增。 |

## 3. 属性与故障测试

- 随机生成 10,000 组 Decimal 运算，与任意精度参考实现比较，断言无 float 路径。
- 对 corpus 行做顺序扰动、重复值、Unicode/空白和平台长度边界，断言 provenance 可唯一复现。
- 在 secret activate 前后注入进程终止，断言 active pointer 只指向验证完成的 generation。
- 并发 50 个 selection 请求，断言相同唯一作用域无重复分配。

## 4. 验收证据

测试报告必须包含 migration schema hash、corpus SHA-256、约束名称、泄漏扫描 sink 列表和 property-test seed。不得把实际完整 secret 写入报告。

## 需求覆盖索引

本测试集覆盖：`FND-001`, `FND-002`, `FND-003`, `FND-004`, `FND-005`, `FND-006`, `FND-007`, `FND-008`, `FND-009`, `FND-010`, `FND-011`, `FND-012`, `FND-013`, `FND-014`。
