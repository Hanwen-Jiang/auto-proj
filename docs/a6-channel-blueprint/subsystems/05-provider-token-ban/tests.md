# S05 Provider Token、Ban Initialization 与 Self-Route Guard：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| BAN-T001 | BAN-001 | schema/service boundaries | Provider Token、ingress Key、callback Token 不能写入彼此表或调用彼此 Saga。 |
| BAN-T002 | BAN-002 | token rotation | rotation command/operation/Outbox 重放幂等；command commit 后 crash 不丢 worker、worker replay 不创建第二 rotation operation。新 Token 的 TOKEN_BAN_INIT（BanRelation READY）、route/cost/model/callability 全确认后才 CAS account binding。 |
| BAN-T003 | BAN-003 | pre-switch compensation | 切换前任一失败删除新 Token，旧 binding 和服务保持。 |
| BAN-T004 | BAN-003 | post-switch orphan | 切换后旧删除失败标 orphan；重启只继续 DELETE，不重绑旧 Token。 |
| BAN-T005 | BAN-004 | `create_missing_ban_relations` | Token × Listing 的 BanRelation 唯一；重复 upsert 不产生重复行。 |
| BAN-T006 | BAN-005 | authoritative set/tombstones | active/down/unavailable/retired/hidden 远端项与本地未获远端删除证明项都留在 owned set；只有 exact delete + complete current Supplier absence 才进入 tombstone。本地 retire/takedown/replacement 不退出。 |
| BAN-T007 | BAN-006,008 | `run_ban_init` TOKEN | 新 Token 对所有 owned_routing_listings 建 BanRelation；generation 变化只补 missing、READY 零重 Ban；relation 全 READY 但任一 Listing/Channel pre-state restore 未 confirmed 时 parent op 仍 PENDING、不得 switch；disable 的 definitive reject/diverged 记录 sticky FAILED，同 operation replay 不自动降回 PENDING；若 cleanup restore 未 confirmed，先 `RETRYABLE_CLEANUP` 到原状态恢复，再暴露 NON_RETRYABLE/可见失败；全部恢复且 generation 稳定后才 READY。 |
| BAN-T008 | BAN-007 | `initialize_listing` | 新 Listing → `init_ck ∈ [0.8,1.9]` Decimal、init price exact readback → temporary resubmit → 无 Marketplace-visible poll → 全部 required BanRelations（含 staged Ban-domain Token）READY → 恢复原状态 → 正常 handoff。 |
| BAN-T009 | BAN-007 | unlisted listing | 业务不可见 Listing 通过 maintenance visibility 临时可见后 Ban，完成后恢复原样，desired 不变。 |
| BAN-T010 | BAN-008 | channel batching | Channel A 初始 DOWN、含 10 Listings：Channel temporary restore=1、restore-to-original takedown=1；在 temporary restore 后 crash，重放仍使用第一次持久化 pre-state=DOWN，不把 UP 当原状态；全部恢复前 admission CLOSED。 |
| BAN-T011 | BAN-009 | event boundary regression | Seller price、请求、cooling、Listing takedown/resubmit 不调用完整 gate，BanRelation 仍 READY；Token onboarding、Channel create/adopt/restore、Provider schedulable 与 max_ratio 更新各调用一次 gate。所有 gate 均不改变 `ban_membership_generation`。 |
| BAN-T012 | BAN-010 | `detect_self_route` | Provider 实际选择 owned_routing_listing 时立即暂停 Provider 并产生 `SELF_ROUTE_DETECTED`。 |
| BAN-T013 | BAN-011 | `repair_ban_matrix` | known Token / known Listing / unknown 三种作用域分别只重建最小矩阵并全部 READY。 |
| BAN-T014 | BAN-012 | token corpus | Token 名来自保存的 corpus index；duty/provider/model/ID 不出现在名称。 |
| BAN-T015 | BAN-004,006,007 | membership concurrency | Token T2 与 Listing L2 并发 enter：四个 membership enter/exit DML 与 generation bump 的 commit-boundary crash 全部满足原子性；并在 matrix READY→Token switch、matrix READY→Listing handoff 两个窗口插入新 member，旧 witness 必须提交失败并补齐新矩阵；最终 T2 × L2 = READY；READY 关系零 full re-Ban。 |
| BAN-T016 | BAN-007 | init_ck replay | LISTING_BAN_INIT 生成 `init_ck = 1.347...`；worker crash 后同 operation replay，init_ck 仍为 1.347...，不得重新 random。 |
| BAN-T017 | BAN-003 | pre-switch membership compensation | 新 Token T2 创建并 readback 后进入 Ban domain（generation 10 → 11）；BAN_INIT 或 route copy 在 SWITCH_BINDING 前不可恢复失败 → delete T2 确认 → T2 移出 Ban domain（generation 11 → 12）。断言：T2 不在 Ban-domain Token 集合、旧 Token 仍 current、后续新 Listing 不要求 T2 的 BanRelation。 |
| BAN-T018 | BAN-003 | compensation unknown delete | delete T2 响应 unknown → T2 仍保持 Ban-domain member、不产生 REMOVED generation、旧 binding 不变，cleanup 在同一 operation 下继续重试；远端确认删除前不得本地移除。 |
| BAN-T019 | BAN-004,005,009 | `run_owned_exclusion_gate` | 完整 Supplier union + 每 Token/model 固定参数完整 search；任一 owned 项缺失/歧义、分页不完整、disabled 两字段不匹配均 BLOCKED。已禁用关系 disable spy=0；注入 enable drift 时仅该关系 disable=1并 exact readback。 |
| BAN-T020 | BAN-009 | minimal persistence | schema 中无 exclusion-attestation 表；gate 只追加现有 operation/audit 与 BanRelation readback reference，返回 READY/阻断。定价、身份、账单 schema/状态机均无 gate hash/generation 字段。 |
| BAN-T021 | BAN-006,009 | activation ordering | Token onboarding 与 max_ratio 更新严格满足 pause/unpublished < full gate READY < Auto-Pool mutation < effective subscription non-empty/disjoint < schedulable；gate 后 membership generation 变化使提交失败并重试。 |
| BAN-T022 | BAN-007,009 | Channel batch | 一个含 30 Listings 的 Channel create/adopt：逐 Listing BAN_INIT 后只执行 1 次全 Token/model gate；普通单 Listing resubmit 的 gate spy=0。 |

## 2. 轮换故障注入

对 CREATE、route copy、BAN_INIT 中间项、switch transaction、DELETE 发包后响应丢失分别终止 worker。断言 operation identity 不变、最多一个 current binding、新 Token 未 ready 时旧 Token 可服务、切换后旧 Token永不成为候选。membership 原子事务在 COMMIT 前 crash 时整体 rollback，replay 必须重新得到 TRANSITION_OCCURRED 且只提交一次 +1；COMMIT 后 crash 的 replay 才返回 NO_TRANSITION 且不重复 +1。另在 `run_ban_init` 返回 READY 后、Token switch/Listing handoff 提交前插入新 Ban member，断言 generation fence 必须使提交失败并回到 matrix reconcile。pre-switch compensation 的 DELETE 响应丢失时，新 Token 保持 Ban-domain member、不产生 REMOVED，cleanup 同 operation 重试。

## 3. Ban 属性测试

- 随机普通事件序列（Seller price、请求、cooling、Listing-scope takedown/resubmit）下 full-gate spy=0；随机失效事件序列精确触发一次 gate。所有 fresh-disabled readback 的 disable mutation spy=0。
- BAN_INIT 每项 readback 两个字段（`user_channel_disabled`、`route_status`）任一缺失时 relation 不 READY。
- 所有事件排列下，不存在新 Token/新 Listing 的 `route_enabled_at < ban_confirmed_at`。
- 随机并发成员变化（新 Token/新 Listing 插入）下，最终矩阵无 missing relation，且已 READY 关系零重 Ban。

## 4. 生产验收证据

对隔离 Provider Token 保存完整搜索参数/页引用、owned_routing_listings 清单、逐项 disable readback 和最终 subscription 无交集证据；使用现有 operation journal/audit，不创建独立证明实体。另保存一次普通 Listing resubmit 的 full-gate spy=0。报告只使用 Token ID、fingerprint/last4。

## 需求覆盖索引

本测试集覆盖：`BAN-001`, `BAN-002`, `BAN-003`, `BAN-004`, `BAN-005`, `BAN-006`, `BAN-007`, `BAN-008`, `BAN-009`, `BAN-010`, `BAN-011`, `BAN-012`。
