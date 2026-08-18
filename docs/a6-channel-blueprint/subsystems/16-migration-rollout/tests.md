# S16 迁移、发布与总验收：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 迁移用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| MIG-T001 | MIG-001 | review gate | 八类评审产物和所有 owner/出口缺一则 implementation gate 失败。 |
| MIG-T002 | MIG-002,003 | `preflight_us_cloud` | 从真实远端发现路径/版本/拓扑，零写操作；Class2 baseline 和 shadow disabled 有证据。 |
| MIG-T003 | MIG-004 | `build_legacy_channel_map` | binding+fingerprint+成功日志唯一选 anchor；并列/矛盾进入 review。 |
| MIG-T004 | MIG-005 | user/channel shapes | 1:1 保 ID/Key；N:1 合并到入口 anchor；1:N 拆用户并逐 Key Saga，唯一有效约束保持。 |
| MIG-T005 | MIG-006 | `migrate_output_group` | 单实体 rename 与双实体 merge 两 fixture；最终仅精确 ToA6API、倍率1、无 alias。 |
| MIG-T006 | MIG-007 | legacy delta | 只给 cutover 当时 candidates 创建 nullable tagged override；之后新 account 无 override。 |
| MIG-T007 | MIG-008 | text/balance | 旧文本只审计；需要新远端写入的字段才 corpus 化；历史 Supplier name 按 `LEGACY_REMOTE` 不再导致迁移失败；旧余额保存，opening adjustment 后新账本从0负扣。migration 完成后 runtime request/scheduler path 的 legacy-map/old-group/opening-adjustment branch spy=0。 |
| MIG-T008 | MIG-009 | input classes | Provider 显式 Class1；current/adoptable Provider Token 在任何 schedulable projection 前必须经 S05 原子 Ban-domain enter，membership 与 generation/`BAN_INIT_REQUIRED` 同事务；OAuth/API Key 无 admin-confirmed contract 时保持 pending。 |
| MIG-T009 | MIG-010 | `cutover_channel` | unlist 后 pre-recovery gate 不要求 marketplace visible；S13 resubmit/VERIFY 后才要求 visible+available+circuit clear。若实现尝试在 unlisted 状态等待 marketplace availability，测试判定为不可达流程失败。 |
| MIG-T010 | MIG-011 | isolation | 一个 Channel gate 失败只回滚其 flag/资源，无关用户账号和其他 Channel 不变。 |
| MIG-T011 | MIG-012 | rollback snapshot | snapshot 覆盖本地 user/key/group 和远端 key/listing state/vector，hash 可验证。 |
| MIG-T012 | MIG-013 | pre-winner rollback | 无 winner 时完整恢复 anchor/current key，不复活非-anchor key。 |
| MIG-T013 | MIG-013 | forward rollback | 有 winner 时保留账本/instance/负余额/current Key，新事实一行不删。 |
| MIG-T014 | MIG-014 | rollback readback | 每个本地/远端恢复项都有 readback；仅 service healthy 不算通过。 |
| MIG-T015 | MIG-018 | Class2 regression | baseline API/config/algorithm/shadow/apply/berserk/probe 与普通流量 golden tests 全绿。 |
| MIG-T016 | MIG-019 | AGENTS evidence | corpus/BanRelation+BAN_INIT/quota/callback/non-quota/shadow 每条硬规则都有测试与生产证据链接。 |
| MIG-T017 | MIG-010 | Exclusion cutover gate | 远端仍存在的 retired/hidden/unavailable 项必须参与；任一 owned 项漏读/未 disabled、subscription 含 owned 或 gate 后 generation 变化时 cutover 失败。cutover 只调用 S05 gate，不实现第二套矩阵扫描。 |
| MIG-T018 | MIG-020 | price order backfill | 历史商户获得确定性初始 `merchant_price_rank`（无随机）；cutover 前管理员确认；rank 不影响 Delta 与风险判定。 |

## 2. 源 Prompt §17 验收矩阵

| 类别 | 主测试包 | S16 集成场景 |
| --- | --- | --- |
| 唯一约束/Decimal/加密/语料/一Channel多Listing | S00-S03 | historical DB migration + secret/text audit |
| zero limits/负余额/models/两类Key | S02,S03,S05,S10 | request auth + ingress/provider rotations |
| Class1/2/3/Auto-Pool/Pin/Ban/liquidity | S04-S06 | route failure + replacement rehearsal |
| price dimensions/FX/hard bounds/8桶 | S07,S08 | real readback fixture + isolated price write |
| delta/transport/winner/earnings/999-1000 | S09-S11 | one delta-required end-to-end request |
| all scheduler modes/三出口 | S09,S12 | parameterized request and fault matrix |
| four-dimensional Listing recovery | S03,S13 | direct relist + cooling reset |
| Outbox/unknown/lease/DLQ/incident | S01,S12-S14 | worker crash and canary incident rehearsal |
| legacy migration/rollback/regression | S15,S16 | canary cutover + pre/forward rollback |

## 3. `us_cloud` 发布测试顺序

1. 只读 preflight 和 deployment manifest diff。
2. 在隔离构建目录运行 migration dry-run、后端/前端/数据库和现有回归。
3. 发布默认关闭 artifact，核对 binary/config/schema，不切流。
4. backfill/shadow-read consistency 和 rollback rehearsal。
5. 单 Channel canary，逐门记录远端 readback。
6. delta-required winner/earnings 闭环与 operation/incident metrics。
7. 批次推进或按 winner 分界执行 rollback。
8. 最终确认 provider shadow timer disabled、旧 alias 与非-anchor Key 未复活。

## 4. 交付证据清单

- 远端 commit/release/schema/deployment manifest。
- 每个 requirement ID 的实现文件、测试 ID 和结果。
- migration 前后计数、conflict/review 清单和 idempotency hash。
- A6 mutation 的 operation ID、pre/post readback hash 和 completeness。
- 同一 request_id 的 delta decision、final usage、本地账单、负余额与 A6 earning。
- full/forward rollback rehearsal journal。
- 所有运行参数默认值、生产 override、owner 和 reload/restart 语义。

完整 secret、Cookie、Session、Authorization 和未脱敏输出不得进入测试 artifact。

## 需求覆盖索引

本测试集覆盖：`MIG-001`, `MIG-002`, `MIG-003`, `MIG-004`, `MIG-005`, `MIG-006`, `MIG-007`, `MIG-008`, `MIG-009`, `MIG-010`, `MIG-011`, `MIG-012`, `MIG-013`, `MIG-014`, `MIG-015`, `MIG-016`, `MIG-017`, `MIG-018`, `MIG-019`, `MIG-020`。
