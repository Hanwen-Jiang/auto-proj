# A6 Channel 分系统实施总纲

> **冻结状态（2026-08-13）**：S00-S16 纵向三联稿、A6 API Reference、实现总合同与实测证据构成冻结终稿。
> 允许的后续修改仅限用户明确授权、当前硬规则直接冲突、已验证 API/事实错误或可复现正确性故障；不得因开放式审计
> 或假设性鲁棒性继续增补。冻结哈希与源文件清单见 [Frozen Manifest](FROZEN-MANIFEST.md)。横向 R0-R4 只做能力投影，
> 不反向修改本终稿。

> 蓝图版本：v1，依据 `Sub2API v0.1.173 原生 A6 Channel 经营模块实现 Prompt` v15。
> 最终实施目标：通过 SSH 别名 `us_cloud` 落实到该机承载的定制版 Sub2API。本文档集本身不授权提前切流、启用 Provider shadow 或修改生产远端事实。

## 1. 总纲用途

本目录把单一大 Prompt 拆成 17 个可独立交付的工作包。拆分依据是数据所有权、远端写入单写者、事务边界、状态机和可独立验证的失败域，而不是原文标题。每个工作包包含互相绑定的三份稿：

- `logic.md`：给人审阅的规范性自然语言蓝图，定义边界、不变量、状态、所有者和验收出口。
- `pseudocode.md`：给实现 Agent 的语言无关伪代码，明确事务、Decimal、CAS、幂等键、读回与补偿顺序。
- `tests.md`：给实现和审查 Agent 的测试合同，每个用例回链需求 ID 和伪代码符号。

本蓝图同时提供一套[横向渐进交付总纲](delivery-slices/README.md)。两套拆分回答不同问题：

- `S00..S16` 是纵向所有权边界，回答“哪一个工作包拥有数据、算法、远端写入和测试合同”。
- `R0..R4` 是横向可上线切片，回答“这一版端到端启用哪些能力，哪些能力必须显式关闭，以及凭什么升级”。

横向切片只编排既有三联稿，不复制第二套算法或状态机。实现时先选目标 release，再从[范围与验收矩阵](delivery-slices/scope-matrix.md)取得该版需要的工作包合同。

发生冲突时的优先级为：根目录 `AGENTS.md` > [原始实现 Prompt](../sub2api-a6-channel-implementation-prompt.md) > [A6 API Reference](../a6-api-reference.md) 与 [错误分类合同](../../errorlist.md) > 本蓝图 > 归档实现。蓝图不得削弱上位合同；发现差异必须先修蓝图和追踪表，再编码。

## 2. 三联稿绑定合同

每个需求使用稳定命名空间，例如 `FND-001`、`CON-004`、`OUT-007`。三份稿必须遵守以下规则：

1. `logic.md` 是该工作包的规范事实，列出全部需求 ID。
2. `pseudocode.md` 的每个入口、状态迁移或关键算法标注其实现的需求 ID。
3. `tests.md` 的每个测试 ID 至少覆盖一个需求 ID，并引用被测伪代码符号。
4. 一个需求只有在单元、集成或故障注入测试中存在可执行证据时才算实现；生产合同另需远端写后读证据。
5. 跨包不变量由一个包拥有，其他包只能消费其版本化事实或命令接口，不得复制第二套状态机。

建议提交时把 `requirements: ["OUT-003", "OUT-004"]` 写入 PR 描述或变更清单，以便按 [需求追踪表](traceability.md) 回溯。

## 3. 工作包索引

| 包 | 子系统与实现边界 | 自然语言 | 伪代码 | 测试 |
| --- | --- | --- | --- | --- |
| S00 | 基础数据、Decimal、凭据安全与公共文本语料 | [Logic](subsystems/00-foundation/logic.md) | [Pseudo](subsystems/00-foundation/pseudocode.md) | [Tests](subsystems/00-foundation/tests.md) |
| S01 | A6 Typed Connector、分页、审计、写后读与结果不明 | [Logic](subsystems/01-a6-connector/logic.md) | [Pseudo](subsystems/01-a6-connector/pseudocode.md) | [Tests](subsystems/01-a6-connector/tests.md) |
| S02 | A6 Channel 出口用户、`ToA6API`、模型解析与稳定绑定 | [Logic](subsystems/02-output-identity/logic.md) | [Pseudo](subsystems/02-output-identity/pseudocode.md) | [Tests](subsystems/02-output-identity/tests.md) |
| S03 | Channel/Listing 生命周期、replacement、退役与入口 Key 轮换 | [Logic](subsystems/03-output-lifecycle/logic.md) | [Pseudo](subsystems/03-output-lifecycle/pseudocode.md) | [Tests](subsystems/03-output-lifecycle/tests.md) |
| S04 | Class1/2/3 输入账号、成本、利润、能力与账单合同 | [Logic](subsystems/04-input-accounts/logic.md) | [Pseudo](subsystems/04-input-accounts/pseudocode.md) | [Tests](subsystems/04-input-accounts/tests.md) |
| S05 | Provider Token、Listing Ban Initialization、事件驱动 Exclusion Gate 与 Self-Route Guard | [Logic](subsystems/05-provider-token-ban/logic.md) | [Pseudo](subsystems/05-provider-token-ban/pseudocode.md) | [Tests](subsystems/05-provider-token-ban/tests.md) |
| S06 | Provider Auto-Pool/Pin、运行失败、流动性与替补 | [Logic](subsystems/06-provider-routing-liquidity/logic.md) | [Pseudo](subsystems/06-provider-routing-liquidity/pseudocode.md) | [Tests](subsystems/06-provider-routing-liquidity/tests.md) |
| S07 | A6 官方价格、Listing 价格向量、markup、ck 与 FX 事实 | [Logic](subsystems/07-price-facts/logic.md) | [Pseudo](subsystems/07-price-facts/pseudocode.md) | [Tests](subsystems/07-price-facts/tests.md) |
| S08 | Listing 定价、Merchant Price Order 与市场分布 | [Logic](subsystems/08-pricing-policy/logic.md) | [Pseudo](subsystems/08-pricing-policy/pseudocode.md) | [Tests](subsystems/08-pricing-policy/tests.md) |
| S09 | Listing-account-model eligibility、delta 策略与 attempt 快照 | [Logic](subsystems/09-eligibility-delta/logic.md) | [Pseudo](subsystems/09-eligibility-delta/pseudocode.md) | [Tests](subsystems/09-eligibility-delta/tests.md) |
| S10 | 经营 delta 与 Class2Adjustment 合成、流式边界和 winner 计费 | [Logic](subsystems/10-usage-billing/logic.md) | [Pseudo](subsystems/10-usage-billing/pseudocode.md) | [Tests](subsystems/10-usage-billing/tests.md) |
| S11 | A6 earnings 收益对账、迟到记录、补账与 Replay 幂等 | [Logic](subsystems/11-revenue-replay/logic.md) | [Pseudo](subsystems/11-revenue-replay/pseudocode.md) | [Tests](subsystems/11-revenue-replay/tests.md) |
| S12 | 请求硬门、错误分层、三出口与平台 Incident | [Logic](subsystems/12-scheduling-incidents/logic.md) | [Pseudo](subsystems/12-scheduling-incidents/pseudocode.md) | [Tests](subsystems/12-scheduling-incidents/tests.md) |
| S13 | Listing hold/desired/observed/plan、下架、冷却、恢复与 Maintenance Visibility | [Logic](subsystems/13-listing-recovery/logic.md) | [Pseudo](subsystems/13-listing-recovery/pseudocode.md) | [Tests](subsystems/13-listing-recovery/tests.md) |
| S14 | Outbox、operation journal、lease、轮询、限流与可观测性 | [Logic](subsystems/14-async-control/logic.md) | [Pseudo](subsystems/14-async-control/pseudocode.md) | [Tests](subsystems/14-async-control/tests.md) |
| S15 | 管理端、read model、权限、预览和人工重放 | [Logic](subsystems/15-admin-console/logic.md) | [Pseudo](subsystems/15-admin-console/pseudocode.md) | [Tests](subsystems/15-admin-console/tests.md) |
| S16 | 旧 Seller/Provider 迁移、`us_cloud` 发布、回滚与总验收 | [Logic](subsystems/16-migration-rollout/logic.md) | [Pseudo](subsystems/16-migration-rollout/pseudocode.md) | [Tests](subsystems/16-migration-rollout/tests.md) |

## 4. 实施顺序

依赖图见 [架构总图与分图](architecture.md#8-实施依赖图)。推荐批次如下：

| 批次 | 工作包 | 退出条件 |
| --- | --- | --- |
| A | S00, S01, S14 | schema/加密/语料、Typed Client 和通用 operation 引擎测试通过，尚无生产写入 |
| B | S02, S04 | 出口与入口身份、绑定、成本和能力事实可 backfill/shadow-read |
| C | S03, S05, S07 | 两套 Key/Token Saga、Token/Listing BAN_INIT、最小事件驱动 Exclusion Gate、价格事实完成写后读沙箱验收 |
| D | S06, S08, S09 | Provider 路由/流动性、定价和 eligibility generation 闭环 |
| E | S10, S11, S12 | usage 合成、唯一 winner 账单、earnings 对账和错误聚合闭环 |
| F | S13, S15 | Listing 单写者恢复与管理端操作只走命令/Outbox |
| G | S16 | 迁移 rehearsal、逐 Channel flag、生产证据和 forward rollback 均可执行 |

同批次可以并行开发，但共享 schema 或接口必须先在 S00/S14 合同中落定。不得为了并行而复制 owner 或跳过 current-generation fence。

## 5. 全局硬门

- 所有金额、倍率、usage 经济判断使用数据库 `NUMERIC` 和应用 Decimal；不允许 `float64` 参与判定。
- A6 凭据 envelope encryption；业务表和日志只存 secret reference、fingerprint、last4 或脱敏摘要。
- 所有远端写入由 owning reconciler 经同一 Outbox 执行，保存写前读、写后读、操作者、部门、generation 和幂等键；**任何 correctness-critical follow-up event 必须与源业务事实在同一数据库事务写入 Outbox，commit 后 notification 仅作唤醒提示。**
- 结果不明必须先 GET；完整分页快照才允许用“未找到”证明删除、恢复或 BAN_INIT/SelfRoute 审计。
- 公共文本只来自版本化仓库语料；语料失败以 `TEXT_CORPUS_INVALID` 阻断相应远端写；远端不可修改的历史 Supplier name 按 `LEGACY_REMOTE` 只审计、不阻塞 ACTIVE。
- Provider Token onboarding、owned Channel create/adopt/restore、Provider 进入/恢复可调度与 Auto-Pool `max_ratio` 更新前必须执行 S05 事件驱动 Exclusion Gate；完整分页逐项读回 disabled，漂移才重发 disable。复用 BanRelation/generation/operation audit，不新增证明表或状态机；普通 Seller 改价、请求、cooling、Listing-scope resubmit 不触发。
- Merchant Price Order（`merchant_price_rank`）固定商户相对价格位置，只由管理员显式调整；Merchant Risk Profile 是事后观测，不回流 S08/S09/S10。
- Provider shadow 默认和最终都保持关闭；只有路由/成本确需新 generation 且无可复用计划时才允许说明理由后单次运行。
- `insufficient_quota` 只进入流动性控制器，不扩 Auto-Pool；非 quota 运行失败才 fresh Observer 并扩至下一完整 CNY 价格带，最高 `0.3`。
- `a6-channel` 请求的初选、粘性、抢槽、failover、retry 与 Replay 全部经过同一 generation/eligibility 硬门。
- `Class2Adjustment` 是 v0.1.173 既有基线；经营 delta 独立决策、一次合成，仅 delta 改变本地 ActualCost。
- hold、desired、observed、plan 正交；只有各自 owner 可清 hold；Listing-scope 上下架只有 Output Recovery 写，Channel-scope takedown/restore/maintenance 只有 S03 写。

## 6. 单工作包 Agent Prompt 模板

将以下模板与目标工作包的三联稿一起交给实现 Agent：

```text
在 ssh us_cloud 上的定制版 Sub2API v0.1.173 中实现工作包 <Sxx>。

先只读确认：实际仓库根、当前版本、工作树、schema 版本、运行 unit、Class2Adjustment 基线和 Provider shadow timer 状态。
按顺序阅读：根 AGENTS.md、原始实现 Prompt、Sxx logic.md、pseudocode.md、tests.md、依赖包公开合同。

只实现 Sxx 拥有的数据和写路径；跨包通过已定义接口/事件/版本化事实连接。不得复制 owner、绕过 Outbox、使用 float 经济判断、
省略远端写后读或用不完整分页证明不存在。先完成迁移与测试，再做默认关闭的接线；没有明确发布授权时不得切生产流量。

交付：代码与迁移、测试输出、需求 ID 覆盖表、远端写后读证据（如该包需要）、回滚步骤、运行参数和剩余风险。
```

## 7. 完成定义

单包完成必须同时满足：

1. 该包全部需求 ID 有实现位置和测试 ID。
2. migration 可前滚，回滚策略明确；历史和 generation 外键不被覆盖。
3. 所有失败分支有 owner、保守状态、自动出口、失败出口和人工出口。
4. 外部写入有可复现的写前/写后事实；结果不明和重放被故障注入覆盖。
5. 日志、trace、错误和审计摘要不泄露完整 Token、Key、Cookie、Session 或 Authorization。
6. 依赖包合同测试与现有 Sub2API/Class2Adjustment 回归测试通过。

整体验收由 S16 汇总，不以“代码已合并”代替生产读回、delta 收益闭环或 forward rollback 证据。

## 8. 相关导航

- [架构总图与分图](architecture.md)
- [横向渐进交付总纲：R0-R4](delivery-slices/README.md)
- [横向范围与验收矩阵](delivery-slices/scope-matrix.md)
- [原始章节到工作包的需求追踪表](traceability.md)
- [原始实现 Prompt](../sub2api-a6-channel-implementation-prompt.md)
- [A6 API Reference](../a6-api-reference.md)
- [错误分类合同](../../errorlist.md)
- [旧 Trader 逻辑说明](../legacy-trader-logic.md)
- [公共文本语料说明](../../resources/README.md)
