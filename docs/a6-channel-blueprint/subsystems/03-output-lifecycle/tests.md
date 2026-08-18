# S03 出口生命周期与入口 Key：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| LIF-T001 | LIF-001 | `plan/try_activate_channel` | 用户/Key 先 staged；任一 LISTING_BAN_INIT/price/capability/eligibility 门缺失时保持 inactive。 |
| LIF-T002 | LIF-002 | `reconcile_channel_apply` | apply response 成功但 inventory 尚无 ID 时 operation 等待；出现唯一新 ID 后才完成。 |
| LIF-T003 | LIF-002 | apply pending reconciliation | `pending_request_exists` 读取并采用唯一现有 request，不重复 POST。 |
| LIF-T004 | LIF-001,003 | `sync_listing_inventory` | 同父 Channel/同 canonical model 的新 remote Listing 保留 stable binding/新 instance；Channel batch 前 Provider 已暂停，全部 Listing BAN_INIT 后只触发一次 S05 full gate。 |
| LIF-T005 | LIF-003 | `sync_listing_inventory` | canonical model 改变时旧 binding 退役、新 binding 新建，历史 attempt 不迁移。 |
| LIF-T006 | LIF-004 | replacement | 父 Channel 消失时调用 supplier/apply；禁止只调用 listing resubmit；新 ID 切 instance。 |
| LIF-T007 | LIF-003,005,006 | `retire_listing` | inflight 非零保持 RETIRE_PENDING；归零后 takedown/readback并本地 RETIRED，但 S05 membership 不退出、不 bump；只有 exact remote delete + complete Supplier absence 才原子退出。 |
| LIF-T008 | LIF-007 | binding adoption | owner/model/channel 唯一自动 adopt；两个候选或 owner 冲突进入 BINDING_REVIEW。 |
| LIF-T009 | LIF-008 | `start/reconcile_key_rotation` | 同 command identity 重放只复用一个 selection/staged Key/parent op。N 个 Listings 只需 1 次 Channel takedown/restore/PATCH；restore 前 Provider pause，restore 后 1 次 Channel-batch gate；gate 失败只保持 Provider paused，新 Key 不回滚，子 Listing mutation=0。 |
| LIF-T010 | LIF-009 | `resolve_key_side` | 每个 phase 注入 crash；重启后远端旧则旧唯一启用、远端新则新唯一启用。 |
| LIF-T011 | LIF-009 | ambiguous key readback | 只有 last4 且 generation 无法区分时 Channel 保持 DOWN、所有 Listing CLOSED、本地不双启用。 |
| LIF-T012 | LIF-010 | old key cleanup | 已确认新 Key 后旧 Key 删除失败只生成 dead-letter，Channel restore 不回退新 Key，也不触发 child Listing recovery。 |
| LIF-T013 | LIF-011 | audit/journal | 每个远端阶段有 pre/post hash、actor、generation、operation ID，日志只有 fingerprint/last4。 |
| LIF-T015 | LIF-008,010,013 | no child recovery spy | Channel 含 30 个 Listings，Key rotation 期间 parent admission=CLOSED 且 replacement planner spy=0；完成后 child Listing takedown calls = 0、child Listing resubmit calls = 0、child observer refresh = yes。 |
| LIF-T016 | LIF-013 | BAN_INIT channel maintenance crash | 原 Channel=DOWN，temporary restore 后 crash；重放复用第一次持久化 pre-state=DOWN，不把临时 UP 当原状态；最终 exact DOWN readback 后才清 maintenance。 |
| LIF-T014 | LIF-012 | corpus gate | 缺 Channel/Listing/Key corpus selection 时不发 apply/update，并投影 TEXT_CORPUS_INVALID。 |

## 2. Saga 故障注入矩阵

在 `STAGE_NEW_KEY`、`CHANNEL_TAKEDOWN` 发包前/发包后响应丢失、`UPDATE_REMOTE_KEY` 发包后、`SWITCH_LOCAL_KEY` 提交前/后、`VERIFY_REMOTE_KEY`、`CHANNEL_RESTORE`、`DELETE_OLD_LOCAL_KEY` 每个点终止 worker。恢复后断言：Channel/child 在确认前 CLOSED；最多一个有效 Key；operation identity 和 selection generation 不变；无重复 Channel takedown/restore/apply。

## 3. Inventory 完整性

- 中间页失败不能判定 Channel/Listing 消失。
- 完整 inventory 中零候选、重复候选、owner 冲突分别进入可解释的 BINDING_REVIEW。
- replacement 成功后旧 instance 可审计但不再 current。

## 4. 生产验收证据

隔离 Channel 执行一次 staged 创建或 replacement rehearsal，以及一次入口 Key 轮换。保存 apply request、inventory 新 ID、Channel takedown/restore、远端 Key 指纹和唯一有效本地 Key 的脱敏 readback。

## 需求覆盖索引

本测试集覆盖：`LIF-001`, `LIF-002`, `LIF-003`, `LIF-004`, `LIF-005`, `LIF-006`, `LIF-007`, `LIF-008`, `LIF-009`, `LIF-010`, `LIF-011`, `LIF-012`, `LIF-013`。
