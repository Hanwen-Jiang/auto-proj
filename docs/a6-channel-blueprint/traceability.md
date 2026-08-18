# 原始 Prompt 需求追踪表

本表证明 [原始实现 Prompt](../sub2api-a6-channel-implementation-prompt.md) 的每个规范章节都有明确工作包，并给出三联稿需求 ID 命名空间。一个章节可以被多个包消费，但只有表中“主 owner”可以定义该状态或远端写入流程。

## 1. 需求命名空间

| 包 | 命名空间 | 主 owner |
| --- | --- | --- |
| S00 | `FND-*` | schema、Decimal、secret、public text selection |
| S01 | `CON-*` | A6 HTTP 合同、分页、readback、transport unknown |
| S02 | `OUT-*` | output identity、group、model binding |
| S03 | `LIF-*` | Channel/Listing instance、ingress Key Saga、retirement |
| S04 | `ACC-*` | Class1/2/3 economic/capability generations |
| S05 | `BAN-*` | Provider Token binding、BanRelation、BAN_INIT 与 SelfRoute Guard |
| S06 | `RTE-*` | Auto-Pool/Pin route、liquidity、replacement |
| S07 | `PRF-*` | official price、FX、markup、confirmed ck facts |
| S08 | `PRC-*` | price bounds、distribution、price write ordering |
| S09 | `ELG-*` | eligibility、delta decision input、attempt snapshot |
| S10 | `BIL-*` | usage compositor、winner、billing intent、stream terminal |
| S11 | `REV-*` | earnings reconciliation、late arrival、Replay settlement |
| S12 | `ERR-*` | request gate、error aggregation、platform incident |
| S13 | `REC-*` | holds、desired/observed/plan、relisting/cooling reset |
| S14 | `ASY-*` | Outbox、lease、journal、polling、rate limits |
| S15 | `ADM-*` | admin read model、RBAC、commands、previews |
| S16 | `MIG-*` | migration、rollout、rollback、production acceptance |

## 2. 源章节覆盖

| 原始章节 | 主 owner | 消费者 | 落地说明 |
| --- | --- | --- | --- |
| 标题、版本、实现依据 | S16 | 全部 | 固定 v0.1.173 与 Class2Adjustment 基线；部署目标为 `ssh us_cloud` |
| §1 实施方式 | S16 | S00, S14 | 评审产物、全栈交付、禁止占位远端写后读/结算/恢复 |
| §2.1 输入/上游 | S04 | S05, S06, S09, S12 | Class1/2/3、route/cost/capability/billing generation |
| §2.2 输出/下游 | S02 | S03, S07-S13 | `a6-channel`、唯一 Key、一 Channel 多 Listing |
| §2.3 连接层 | S09 | S02, S04, S10 | 稳定 binding 解析、attempt 冻结 generation |
| §3 A6 Typed Client | S01 | 所有远端写包 | 鉴权、encryption reference、分页、读回、unknown result、白名单控制台 |
| §4 核心数据模型 | S00 | 全部 | 基础实体和唯一约束；各业务包拥有自身表的状态语义 |
| §4.1 公共文本语料 | S00 | S03, S05, S15, S16 | 语料 provenance、冲突重选、`TEXT_CORPUS_INVALID` |
| §5 输出 Channel 用户 | S02 | S03, S09, S13, S16 | 用户、模型解析、负余额例外、`ToA6API`、binding 生命周期入口 |
| §6.1 输出入口 Key | S03 | S13, S14 | Channel-scope ingress Key rotation：Channel takedown → PATCH api_key → restore；唯一有效 Key、清理 dead letter |
| §6.2 Provider 路由 Token | S05 | S06, S14 | 新旧 Token Saga、切换、删除 readback、orphan cleanup |
| §7.1 Class1 | S04 | S05, S06 | Auto-Pool/Pin `sk`、Top-N 统计与 cost generation |
| §7.2 Class2 | S04 | S09, S10 | weekly quota Decimal、100% 不暂停、缺失保留历史 |
| §7.3 Class3 | S04 | S09, S12 | declared sk、模型覆盖、账单偏差窗口、恢复 gate |
| §8 Auto-Pool 与 Ban | S05 | S03, S06, S14, S16 | authoritative owned set、remote-delete-only tombstones、Token/Listing BAN_INIT、事件驱动 Exclusion Gate、SelfRoute repair |
| §9 ck、官方价格与 FX | S07 | S08-S11 | micros 向上量化、markup、事实源和 FX fallback |
| §10 定价范围与分布 | S08 | S09, S13 | hard bounds、Class1 floor、8 桶、Merchant Price Order、稳定匹配、改价先后 |
| §11 eligibility 公式 | S09 | S10, S12, S13 | delta override、transport profile、sparse projection |
| §11.1 经营 delta | S10 | S09, S11, S12 | winner、solver、ActualCost、usage 边界、synthetic SSE prelude 与 upstream stream commit 分界 |
| §11.2 Class2Adjustment | S10 | S16 | 保持现有算法/API/rules/shadow/apply/berserk/probe 兼容 |
| §11.3 合成与核对 | S10 | S11 | 两决策同读 U0、一次合成、比例对账与偏差作用域 |
| §12 调度与错误 | S12 | S04, S06, S09, S10, S13 | 同门、三出口、attempt 与聚合终态分层；SSE 500-2000ms 单次 prelude 与 pre-commit candidate failover |
| §12.1 平台 Incident | S12 | S13, S14 | fingerprint、提升阈值、scope member、canary 编排 |
| §13 Listing 状态与恢复 | S13 | S03, S09, S12, S14 | hold owner、四维状态、Listing-scope 唯一写者、maintenance visibility、resubmit/cooling 合同 |
| §14 事件与 Reconciler | S14 | 全部 | Outbox、锁序、lease、频率、429、去重、dead letter |
| §15 管理端 | S15 | 全部 | 部门边界、read model、参数编辑、Merchant Price Order 管理、Risk Profile 展示、语料选择（含 LEGACY_REMOTE 只读）、重放 |
| §16 迁移 | S16 | S02-S15 | Seller 聚合/拆分、组 canonicalize、账本、flag、rollback |
| §17 验收 | S16 | 每个 `tests.md` | 单包验收在本包，端到端与生产证据由 S16 汇总 |

## 3. `AGENTS.md` 硬规则覆盖

| 硬规则 | 主 owner | 必须出现的证据 |
| --- | --- | --- |
| Provider shadow 默认禁用、只在无可复用 generation 时单次运行 | S06, S16 | timer disabled readback；若例外，记录具体 generation 理由 |
| 公共文本全部来自仓库语料 | S00 | SHA-256、sheet/row 或 index、selected value、selection generation |
| Provider owned-Channel self-route exclusion | S05 | Supplier union local owned set、固定参数完整 Marketplace 分页、逐关系 exact disabled readback、drift-only disable、effective subscription 无 owned、membership fence；无第二 attestation 表 |
| `insufficient_quota` 进入 liquidity，不走 Observer 扩池 | S06 | account pause、UNFUNDED、whole-unit transfer/replacement journal |
| liquidity/callback Token 独立且只 Pin 合格小商户 Channel | S06 | 独立 duty/binding、目标 listed+callable readback |
| 生产 callback 使用唯一固定 PING 且拒绝 probe-shaped usage | S06 | exact input hash、realistic usage 与 high-cache witness |
| 非 quota Provider 失败扩到下一 CNY 价格带且不超过 0.3 | S06 | fresh Observer、strict expansion、matching USD cost generation、Seller trigger |

> `AGENTS.md` 根文件本身不在本合集内；本行记录其 owned self-route 原则的现行版本，供实现 Agent 对照根文件核对：
>
> `BanRelation = provider_token_instance × owned_listing_instance` remains the only durable Ban business state.
> The owned set includes every remotely present active/down/unavailable/retired/hidden item and every locally
> controlled item without exact remote-delete proof. Run one complete gate on Token onboarding, owned Channel
> create/adopt/restore, Provider schedulable activation, every Auto-Pool max_ratio update, and SelfRoute repair.
> Already-disabled relations require fresh readback but zero mutation. Ordinary Seller price/request/cooling and
> Listing-scope resubmit do not run the gate. `ban_membership_generation` fences concurrent set changes.
> New Listing BAN_INIT uses persisted Decimal `init_ck` randomly selected in `[0.8, 1.9]`
> against current confirmed official-price generation.
> BAN_INIT does not wait for Marketplace visibility before disable;
> disable/readback itself is the readiness proof.

## 4. 跨包状态所有权

| 状态/事实 | 唯一 owner | 其他包允许的动作 |
| --- | --- | --- |
| secret generation | S00 | 只按 reference 读取授权后的 secret |
| remote observed fact/completeness | S01 | 消费带时间和 generation 的读回，不自行猜测 |
| output binding/current instance | S02/S03 | 使用稳定 ID；不得覆盖历史 instance |
| account economic/capability generation | S04 | S09 只消费 current confirmed generation |
| BanRelation / BAN_INIT / Exclusion Gate | S05 | S03/S06/S16 只触发并消费 READY/阻断；S08/S09/账单不感知 gate，不复制内部证据或状态 |
| Ban membership generation（ban_membership_generation） | S05 | S03/S13/S16 只消费或触发成员事件，不得自行递增/修改 generation |
| Merchant Price Order / merchant_price_order_generation | S08 | S15 只提交显式管理命令与读取 read model；Risk Profile 不得作为输入 |
| route generation/UNFUNDED | S06 | scheduler 只消费 schedulable projection |
| confirmed price/FX generation | S07 | S08-S11 不写第二套价格事实 |
| desired price generation | S08 | S13 只按当前 generation 协调上下架 |
| eligibility/delta snapshot | S09 | scheduler/计费不可临时重算历史 attempt |
| winner/billing intent | S10 | S11 只推进同一 intent 的对账状态 |
| reconciliation/late adjustment | S11 | S10 不重复扣费 |
| error aggregate/incident | S12 | S13 只消费 membership/witness，不改 classification |
| holds/desired/observed/plan | S13 各字段指定 owner | 管理端发命令，不直接篡改 observed |
| operation/lease/retry/dead letter | S14 | 业务包提供 handler，不另建任务系统 |

## 5. 审查方法

1. 从源章节选择一行，进入主 owner 的 `logic.md` 并找到对应需求 ID。
2. 在同目录 `pseudocode.md` 找到实现该 ID 的符号和事务/失败分支。
3. 在 `tests.md` 找到至少一个正常路径和一个适用的失败/重放路径。
4. 跨包要求再检查本表的唯一 owner，拒绝重复状态和双写。
5. 发布前由 S16 汇总所有需求 ID、测试 ID、迁移、远端 readback 与回滚证据。

## 6. 原始实现依据路径映射

原 Prompt 记录的是设计时 Mac 工作区路径；它们用于来源审计，不得在 `us_cloud` 上当作真实部署路径。S16 必须只读发现远端源码与 release 根。

| 原始路径 | 当前权威位置/处理 |
| --- | --- |
| `/Users/haven/Documents/code/codex-pool/sub2api-update-20260811-v0173` | `us_cloud` 上由 S16 preflight 发现并核对 v0.1.173/commit，不猜路径 |
| `/Users/haven/Documents/code/a6api-auto/archive/legacy-trader` | [冻结归档](../../archive/legacy-trader/) |
| `/Users/haven/Documents/code/a6api-auto/docs/a6-api-reference.md` | [活跃 A6 API Reference](../a6-api-reference.md) |
| `/Users/haven/Documents/code/a6api-auto/api-docs/7-24-15-54.md` | [A6 抓包](../../api-docs/7-24-15-54.md) |
| `/Users/haven/Documents/code/a6api-auto/api-docs/api-supplier-profile.md` | [Supplier Profile 抓包](../../api-docs/api-supplier-profile.md) |
| `/Users/haven/Documents/code/a6api-auto/api-docs/api-status.md` | [A6 Status 抓包](../../api-docs/api-status.md) |
| `/Users/haven/Documents/code/a6api-auto/errorlist.md` | [错误分类合同](../../errorlist.md) |
| `/Users/haven/Documents/code/a6api-auto/AGENTS.md` | [生产与安全硬约束](../../AGENTS.md) |

返回 [分系统实施总纲](README.md)。
