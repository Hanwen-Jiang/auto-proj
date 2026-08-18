# R0-R4 范围与验收矩阵

[横向总纲](README.md) | [纵向分系统总纲](../README.md) | [需求追踪](../traceability.md)

## 1. 读表规则

| 标记 | 含义 |
| --- | --- |
| `M` | 本 release 的支持面必须实现并验收该包所列范围 |
| `E` | 只实现本格明确写出的窄子集；未列能力保持关闭 |
| `OFF` | 功能必须显式关闭并有负向测试，不能落回旧路径 |
| `C` | 继承前一版已经验收的能力，并做回归 |
| `FULL` | 该纵向包全部 requirement、伪代码和 tests 达到最终合同 |

`E` 不代表某个纵向 requirement 已完成。只有一个 requirement 的全部规范、伪代码入口和测试合同都实现后，追踪系统才能标为 complete；横切稿不得把半个 requirement 记成通过。

## 2. 子系统增量矩阵

| 包 | R0 受控闭环 | R1 可运维输出 | R2 鲁棒路由 | R3 经营自动化 | R4 最终完整 |
| --- | --- | --- | --- | --- | --- |
| [S00](../subsystems/00-foundation/logic.md) | `M` stable binding、Decimal、secret/redaction、immutable generation | `M` public corpus、完整 output instance/key 基础实体 | `M` Provider Ban/route/capability 基础实体 | `M` Merchant Price Order、price/delta/revenue 实体 | `FULL` 全 schema/迁移/约束 |
| [S01](../subsystems/01-a6-connector/logic.md) | `E` 本版只读、Listing takedown/resubmit、分页 completeness、unknown/readback | `M` Channel/Listing/Key/price 所需 mutation surface | `M` marketplace/Token/Pin/Auto-Pool/circuit/balance surface | `M` official price/earnings/settlement/withdraw surface | `FULL` 全确认 API 与控制台白名单 |
| [S02](../subsystems/02-output-identity/logic.md) | `M` 一 Channel/一 Listing/一 model 的完整请求身份链 | `M` 多 Channel/多 Listing/model、完整 group drift | `C` 多 credential 与全 candidate projection | `C` 价格/eligibility generation 接线 | `FULL` legacy alias 清零与全部边界 |
| [S03](../subsystems/03-output-lifecycle/logic.md) | `E` adopt 既有实例、只读 Key 验证、紧急 Channel/Listing 停用边界 | `M` create/adopt/replace/retire、入口 Key Saga、maintenance | `C` 与 Ban membership/incident 协调 | `C` 与改价/恢复编排协调 | `FULL` 全长尾、orphan、迁移实例 |
| [S04](../subsystems/04-input-accounts/logic.md) | `E` Class2 cost/capability/schedulability，固定 allowlist | `C` 多 Class2 和多模型 | `M` Class1/2/3 全成本、能力、账单 generation | `C` 供 pricing/delta 消费 | `FULL` 全边界与历史 backfill |
| [S05](../subsystems/05-provider-token-ban/logic.md) | `E` Provider Token 域必须为空；零远端 Ban worker | `E` Provider Token 域为空；新 Listing 只执行空 Token 域 `LISTING_BAN_INIT`，零 full gate | `M` BAN_INIT + 最小事件驱动 Exclusion Gate + SelfRoute repair | `C` 仅冻结触发事件运行 gate；price/request 不触发 | `FULL` migration/tombstone/并发与成本上限证据 |
| [S06](../subsystems/06-provider-routing-liquidity/logic.md) | `OFF` | `OFF` | `M` Token、Pin/Auto-Pool、quota liquidity、replacement、non-quota expansion | `C` 与 Seller repricing 触发接线 | `FULL` 全自动化和规模证据 |
| [S07](../subsystems/07-price-facts/logic.md) | `E` 读取并冻结人工确认的现有 Listing vector、delta=0 可盈利门 | `M` 固定目标价写后读、基础 official/model price/FX 事实 | `C` route cost 与多商户 confirmed facts | `FULL` 官方价/FX/markup/ck 全事实链 | `FULL` 回归与历史完整性 |
| [S08](../subsystems/08-pricing-policy/logic.md) | `OFF` 自动定价 | `E` 管理员给定固定价，只校验硬边界 | `E` 多商户固定价格，仍不自动分布 | `FULL` Merchant Price Order、分桶、稳定匹配、写序 | `FULL` 规模/漂移/迁移证据 |
| [S09](../subsystems/09-eligibility-delta/logic.md) | `M` 精确 binding、delta 固定 0、`ck>=required`、attempt snapshot/current gate | `M` 多 Listing/transport profile，delta 仍固定 0 | `M` Class1/2/3、所有 scheduler 入口统一硬门 | `FULL` delta auto/override、全 generation 快照 | `FULL` 全 replay/迁移/规模证明 |
| [S10](../subsystems/10-usage-billing/logic.md) | `M` delta=0、保留 Class2Adjustment、唯一 winner/intent/负账单、SSE commit 边界 | `C` 多 Listing 幂等与 crash recovery | `M` 多候选 failover 与 delivery unknown 完整处理 | `FULL` 两策略合成、solver、delta ActualCost | `FULL` 所有 schema/endpoint/长尾回归 |
| [S11](../subsystems/11-revenue-replay/logic.md) | `E` request_id earnings correlation、重复幂等、人工异常队列 | `M` 自动轮询、late arrival、基础 Replay/VOID | `M` 多上游 settlement、unknown 补漏 | `FULL` delta/Class2Adjustment 比例核对、作用域修复 | `FULL` 全量 sweep/历史/规模证据 |
| [S12](../subsystems/12-scheduling-incidents/logic.md) | `M` USER/NO_ELIGIBLE/busy/attempt failure、单上游 SSE 不拼接 | `M` aggregate finalizer 与 Listing cooldown 基础 | `FULL` prelude、pre-commit failover、platform Incident/canary | `C` 经济异常作用域接线 | `FULL` 全 errorlist 与故障注入证据 |
| [S13](../subsystems/13-listing-recovery/logic.md) | `E` 单 Listing 紧急 hold/unlist、人工恢复、精确 readback | `M` desired/observed/plan、自动 cooling/recovery、maintenance | `FULL` Incident/Ban/多 Channel 编排 | `C` price/eligibility hold 接线 | `FULL` 全长尾和批量恢复证据 |
| [S14](../subsystems/14-async-control/logic.md) | `M` 最小 Outbox、operation journal、lease、去重、dead letter 可见 | `M` 所有 output mutation owner 与轮询 | `FULL` route/liquidity/incident 并发和限流 | `C` pricing/revenue worker 扩展 | `FULL` HA、backpressure、规模证明 |
| [S15](../subsystems/15-admin-console/logic.md) | `E` 只读 canary 状态、kill switch、异常与手工恢复命令 | `M` output lifecycle/corpus/fixed price/hold/replay 界面 | `M` Provider/route/incident 管理与证据 | `FULL` pricing/delta/revenue preview 与 Merchant Price Order | `FULL` Risk read model、RBAC 与全部操作 |
| [S16](../subsystems/16-migration-rollout/logic.md) | `E` adopt-only 单 Channel、deployment manifest、逐 Channel flag、两类 rollback | `M` 同一商户 output 资源 create/adopt 与运维发布 | `M` 多商户/Provider rollout 与回滚 | `M` 经营策略按现有 generation 发布 | `FULL` 全 legacy N:1/1:N 迁移、全证据总验收 |

## 3. 能力首次开放点

| 能力 | 首次允许 | 开放前置门 |
| --- | --- | --- |
| 既有 A6 Channel 真实请求 | R0 | 单 Channel allowlist、精确 Key/model binding、Class2 delta=0 可盈利、rollback snapshot |
| Listing 自动停用 | R0 | S13 单写者、S14 Outbox、S01 exact readback |
| Channel/Listing 创建和公共文本写入 | R1 | 完整语料 provenance、生命周期 Saga、unknown/readback、orphan 出口 |
| 入口 Key 轮换 | R1 | Channel takedown、唯一 effective Key、PATCH readback、restore gate |
| 多 Channel/多模型 | R1 | stable binding、独立 admission/hold、per-Channel flag/rollback |
| Class1 Provider | R2 | 全 owned inventory ban、route/cost/capability readback、shadow disabled |
| Class3 | R2 | declared cost、model probe、billing deviation pause/recovery |
| SSE candidate failover | R2 | classifier 复用、request-local prelude、upstream commit fence、每次重过 S09 |
| 自动 Listing 价格分布 | R3 | official price/FX、Merchant Price Order、hard bounds、write ordering/readback |
| 非零经营 delta | R3 | transport profile、frozen integer vector、deterministic solver、earnings reconciliation |
| 全 legacy 迁移 | R4 | N:1/1:N mapping rehearsal、opening adjustment、alias/Key 清理、forward rollback |

## 4. 每版集成验收对象

横切集成测试采用 `R*-E2E-*` 命名；它们不替代纵向测试，只证明多个 owner 的组合闭环。

| Release | 必过集成对象 |
| --- | --- |
| R0 | 单 Channel 成功闭环、SSE/non-stream、零资格、全忙、上游失败、重复 terminal、earnings 缺失/迟到、两类 rollback |
| R1 | 创建/adopt/replace/retire、Key Saga、语料失败、固定价写后读、hold/recovery、worker crash/unknown |
| R2 | Provider 全量 self-route Ban、Pin/Auto-Pool、quota/non-quota 分流、Class3 pause、pre-commit failover、Incident canary |
| R3 | 价格分布、Merchant rank 稳定性、delta 999/1000、两策略合成、solver、收益比例、Replay/adjustment |
| R4 | 全部冻结 requirements/纵向 tests（数量从 trace manifest 生成，不硬编码）、旧系统全迁移、HA/限流、生产批次、两类 rollback |

## 5. Release 完成口径

某版只有在其 `M/E` 范围通过、`OFF` 范围有负向证据、前版回归通过、release E2E 通过并完成一个受控 Channel 的发布/回滚 rehearsal 后才可以标记完成。若 R0 已上线但尚未实现 R1，不得把 R1 的缺失计为 R0 defect；若 R0 收到了其 manifest 外的 Class1、未知 endpoint 或第二个 Channel 请求而仍继续调度，则属于 R0 的 fail-closed defect。
