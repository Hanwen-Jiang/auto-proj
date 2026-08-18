# S08 Listing 定价策略与分布：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| PRC-T001 | PRC-001,010 | `compute_band/apply_pricing_policy_command` | 默认值正确；min<=0、min>max、max>4 拒绝；合法 hard-bound admin command 只由 S08 写 generation，并与 pricing-replan Outbox 同事务；若新 bound 立即使 current confirmed_ck 越界，同事务产生 clipped correction desired generation + admission safety fence，不能等待异步 planner 后才 CLOSED。 |
| PRC-T002 | PRC-002 | `compute_band` | required profiles 全 capable cap=.5；任一不 capable cap=0；空集合 config invalid。 |
| PRC-T003 | PRC-003 | class1 floor | 只使用 full ready Class1；满并发仍参与，BAN_INIT/route/capability 失效则排除。 |
| PRC-T004 | PRC-004 | bounds | 无 ready 用 hard min；raw>max 固定 max 且不生成>4；不在本包擅自下架。 |
| PRC-T005 | PRC-005 | `build_targets` | 只采外部 callable 非 owned，公开 k 精确除 1.2，构造 8 log buckets。 |
| PRC-T006 | PRC-006 | largest remainder | 分配数总和等于 Listing 数，按频率和稳定 tie-break；桶内来自实际分位点。 |
| PRC-T007 | PRC-006 | stable matching | 商户内 current ck 按 `(current_ck,stable_binding_id)` 排序、target 升序后一一 zip；同输入/不同遍历顺序得到相同 binding->price，`Σ|new-current|` 不高于任意随机/逆序对照；实现不得调用 Hungarian/min-cost-flow/general assignment。 |
| PRC-T008 | PRC-007 | zero/single-point | **complete** snapshot 零样本不调用 log 且仍经 Merchant Price Order；incomplete/failed snapshot 不得走 zero-sample、price-write spy=0；lower=upper 全部单点。 |
| PRC-T009 | PRC-008 | batching | 第 11 项进入下一批；5 分钟未到的普通改价不写。 |
| PRC-T010 | PRC-009 | down ordering | 先发布保守 generation，再等待旧 inflight，归零后才远端 PUT。 |
| PRC-T011 | PRC-010 | down timeout | 普通降价超时取消并恢复；hard-max breach 保持 CLOSED 且继续重试。 |
| PRC-T012 | PRC-011 | up ordering | 涨价远端 readback 前不发布允许降低 delta 的 generation。 |
| PRC-T013 | PRC-012 | write outcome | target/old/third/reject/incomplete 分别 adopt/replay/replan/fail/retry-readback；期间禁止后续调价；confirmed path 只产生一个 S07 confirmed price generation。 |
| PRC-T014 | PRC-006 | merchant price order | Merchant A rank < Merchant B rank 时 A 获得的目标价格槽不能整体高于 B（`max_ck(A) <= min_ck(B)`）。 |
| PRC-T015 | PRC-006 | market shift | 市场整体上涨/下降后，商户间相对价格顺序不变；管理员改 rank 才改变顺序。 |
| PRC-T016 | PRC-006 | per-merchant matching | 商户内部满足 minimum-movement；相同输入重放 mapping 完全 deterministic。 |

## 2. 属性测试

- 对随机 bounds、样本和 Listings，所有目标在 bounds 内且数量完全守恒。
- 最大余数分配误差每桶小于 1 个 Listing。
- 绝对不变量：任意 merchant 分组与 rank 排列（含零样本与单点路径）下，商户间 `max_ck(Mi) <= min_ck(Mj)` 对 `rank_i < rank_j` 恒成立。
- 重放相同 generation 的 plan hash 完全一致。
- hard-bound / Merchant Price Order command 在 generation+Outbox commit 后、phase journal 确认前 crash：same `op.identity` 只 ADOPT 同一 generation；更晚 current 存在时旧 op SUPERSEDED。
- 任意 crash 点不存在本地假设 ck 高于远端 confirmed 的窗口。

## 3. 端到端验收

用至少 8 个受管 Listing 的脱敏市场 fixture 验证桶分布、稳定匹配和批次；隔离 Listing 各执行一次涨价与降价，保存 generation 顺序、inflight drain、A6 price vector readback 和 S07 confirmed ck。

## 需求覆盖索引

本测试集覆盖：`PRC-001`, `PRC-002`, `PRC-003`, `PRC-004`, `PRC-005`, `PRC-006`, `PRC-007`, `PRC-008`, `PRC-009`, `PRC-010`, `PRC-011`, `PRC-012`。
