# A6 Trader 定价与动态 Pin 规范

状态：规范性文档
适用模型：试点阶段仅 `gpt-5.6-sol`
默认展示坐标：`K_CNY`
公式版本：`pricing-contract-v3-landed-cost-delta-decay`
最后核对：2026-07-28

## 0.A 2026-08-04 运行时利润率修订

当前生产目标成本加价率由模型级 PostgreSQL 配置 `a6_trade.model_profit_settings` 管理，
`gpt-5.6-sol` 当前为 `m=0.60`。因此联合硬底线为：

```text
F_target = (1 + 0.60) * K / (5/6) = 1.92K
```

Dashboard 输入和持久化的是 `60%`，`1.92x` 仅作为包含平台换算后的只读毛价硬下限展示。本文其余
`m=0.20/1.44K` 数字保留为公式样例与最低允许边界；运行时计划必须使用模型配置覆盖该旧默认。

> 本文是 A6 Trader 定价实现、审计和验收的唯一公式来源。其他文档、代码注释或面板文案
> 与本文冲突时，以本文的变量定义、运行时验证和验收断言为准。任何公式变更必须先修改
> 本文、补齐反例和单元测试，再进入生产写入。

## 0. 本次修订结论

线上 A/B 请求已经证明 Enodot Key 满足
`actual_cost = total_cost * (1 + delta)`：`delta=0` 时倍率为 `1.0`，`delta=0.5` 时倍率为
`1.5`。第二次现场复核确认，Observer/Pin 中的 `K` 是上游原始价值倍率，Provider 实际拿货
成本还要经过平台换算：`C_landed=K/c`。上一版把 `K` 直接当成实际拿货成本，少除了一次
`c=5/6`，因而把 20% 利润底线从正确的 `1.44K` 错算成了 `1.2K`。

生产定价必须直接执行以下联合规则：

1. 实际拿货成本为 `C_landed=K/c`；当 `c=5/6` 时，`C_landed=1.2K`。
2. 最终利润底线为 `F_target=(1+m)C_landed=(1+m)K/c`；当 `m=0.2` 时为 `1.44K`。
3. 最低面板价为 `P_min=F_target/(1+delta_max)`；默认 `delta_max=0.5`，所以
   `P_min=0.96K`，公开价 `L_min=1.152K`。
4. 在 `P in [0.96K,1.44K]` 内，价格变化只通过反解 `delta=1.44K/P-1` 补偿，最终
   `F=(1+delta)P` 始终为 `1.44K`，相对实际拿货成本 `1.2K` 的利润为 20%。
5. 当 `P>=1.44K` 时 `delta=0`；继续提高价格直接形成高于 20% 的利润。
6. 当 `P=K` 时，`delta=1.44K/K-1=0.44`。这是 `100 -> 实付120 -> 目标收益144`
   的固定回归样例。

用业务讨论中的 `a/d/x` 记号表达同一规则：`a=K_quote` 是混合 Pin 的加权平均成本，
`d=1+delta_configured` 是 Enodot 实际乘数，`x=P/a` 是我方 Listing 相对于 `a` 的倍率。于是：

```text
a * d * x = 1.44a
d * x = 1.44
x = 1.44 / d
P = a * x
```

这里的 `1.44` 是相对 `a` 的归一化目标，完整量纲是 `1.44a`。该关系是最低 20% 的定义；不得替换成
Provider pool 的 `billing_floor >= 1.20*K_pool`，也不得替换成历史窗口聚合收入必须覆盖全部运行成本
`1.20` 倍的模型级上下架门禁。

`c=5/6` 只表示同一平台价格坐标的换算关系，不是另一笔 `settlement_fee`。在 `1.44a` 之外不得再扣
一次 `1/6`、不得重复乘平台费，也不得把该换算系数另立为利润门禁。

实际现金实收仍作为独立经营指标 `F_realized` 记录和对账，但不再把 managed Key 的客户类型
作为强制清零 `delta` 的定价开关。只有 delta 能力、写入或读回失败时，才停止本次降价写入。

## 1. 目标与不可破坏的约束

系统要同时完成四件事：

1. Provider 优先从低价、健康、低延迟的外部渠道拿货。
2. Seller 在 A6 市场覆盖一个有密度的价格区间，而不是全部挤在一个价格点。
3. Enodot 单 Key 的 `delta` 与 A6 Seller 面板价协同，使低价档仍达到最低收益要求。
4. 任意时刻都能从真实 Provider 成本、A6 Listing 写入值、Enodot 倍率后扣费和外部现金
   结算证据复算利润。

以下约束属于硬断言：

- Provider Key 永远排除我方全部渠道，不得 Pin 到自己或其他受管 Seller。
- 最低成本加成率默认 `m_min = 20%`。
- 文中的“20% 利润”统一指 `收益 / 成本 - 1 = 20%`；对应的收入毛利率是
  `(收益 - 成本) / 收益 = 16.6667%`。
- 所有价格参与计算前必须转换到同一模型、同一币种、同一计价项和同一时间窗口。
- 控制器只使用 API 原始数值和整数 micros，面板四舍五入后的 `x0.xxxx` 只用于展示。
- 任何会降低安全利润的取整一律向上取整。
- `delta` 目标值必须先写入并读回，再允许写入依赖该 delta 的更低 Listing 价格。
- Pin、`delta` 或 Listing 写入结果不明确时，保持或恢复较高的安全价，不带着未知状态继续降价。

## 2. 统一价格坐标与变量

### 2.1 唯一计算坐标

控制器中的 `K`、`C_landed`、`P`、`L`、`F` 全部是同一 `K_CNY` 坐标中的无量纲价格倍率。原始 API
中的 `K_USD`、大厅 `x...` 展示倍率、人民币报价和 Listing micros 必须先通过带版本的坐标
适配器转换，禁止直接混算：

```text
Q_i = normalize_to_k_cny(raw_quote_i, quote_coordinate, fx_snapshot, model_price_version)
```

模型真实价格是四项向量，而不是只有输入价：

```text
price = (input, output, cache_read, cache_write)
```

试点阶段只有在四项价格都与官方价格向量同比例时，才用一个标量代表整个向量。一旦输出或缓存
比例不同，成本和收入都按真实请求 token mix 逐项计算。

对计价项 `t` 定义：

| 变量 | 含义 | 单位 |
| --- | --- | --- |
| `B_t` | 模型计价项 `t` 的官方基准价 | micros / 对应 token 单位 |
| `Q_i` | 外部渠道 `i` 归一到 `K_CNY` 后的上游原始价值倍率 | 无量纲 |
| `K_quote` | 多个当前 Pin 的加权上游原始价值倍率 | 无量纲 |
| `c` | A6 面板价到大厅公开价的结算换算因子，当前 `5/6` | 无量纲 |
| `C_quote=K_quote/c` | 按报价计算的 Provider 预计实际拿货成本 | 无量纲 |
| `C_realized` | Provider 实际扣费回读出的已实现拿货成本 | 无量纲 |
| `C_safe` | 加入实付回读和不确定性后的安全拿货成本 | 无量纲 |
| `K_safe=c*C_safe` | 为展示而换回上游原始倍率坐标的安全值 | 无量纲 |
| `P` | A6 Seller 后台持久化的面板倍率 | 无量纲 |
| `L = P / c` | 与外部 `Q_i` 同坐标的我方大厅公开倍率 | 无量纲 |
| `delta_configured` | Enodot 单 Key 配置的附加倍率，范围 `[0, 0.5]` | 无量纲 |
| `delta_effective` | 本轮目标且写入后已读回的附加倍率；规划阶段等于目标 `delta` | 无量纲 |
| `F_expected = (1+delta_effective)P` | 定价控制器认可的预期最终收益倍率 | 无量纲 |
| `F_realized` | 外部已结算现金收入除以同口径官方基准计价量 | 无量纲 |
| `m` | 目标成本加成率 | 无量纲 |

Listing 写入边界才把倍率转换为整数价格：

```text
listing_price_micros_t = ceil(B_t * P)
P_effective_t = listing_price_micros_t / B_t
```

所有利润断言使用取整后的 `P_effective` 和真实 token mix 再验一次。`ceil` 保证价格取整不会
压低收入；Provider 成本一侧则使用实际扣费回读，不用展示值反推。

### 2.2 A6 面板价与大厅价

换算方向固定为：

```text
L = P / c
P = c * L
c = 5/6 = 0.833333333333...
```

因此：

- 外部渠道原始倍率为 `K` 时，Provider 实际拿货成本是 `C_landed=K/c`；`K=100` 对应
  实付成本 `120`。
- 我方 Seller 写入 A6 后台的是 `P`；大厅看到的是更高的 `L = P / c`。
- `c` 同时用于同一种平台结算关系：`K -> C_landed` 与 `P -> L`；它不承担币种或页面展示
  坐标转换。

若 A6 页面显示的 `x...` 使用另一坐标，单独保存该观测窗口的转换系数 `g_display_to_k`：

```text
K_CNY = g_display_to_k * R_display
R_display = K_CNY / g_display_to_k
```

`g_display_to_k` 必须来自同一模型、同一计价项、同一时刻的 API 原始字段和汇率快照，并保存
`fx_snapshot_id` 与 `coordinate_version`。它与 `c=5/6` 是两种独立换算，任何实现都不得合并
或互相替代。

### 2.3 Enodot `delta`

下文用 `delta` 简写当前 `delta_configured`。线上用量日志已经验证：

```text
rate_multiplier = 1 + delta
actual_cost = total_cost * rate_multiplier * account_rate_multiplier
```

当前受管 Key 的 `account_rate_multiplier = 1`，所以可简化为：

```text
actual_cost = total_cost * (1 + delta)
```

定价合同把同一 Listing 的联合最终倍率定义为 `F=(1+delta)P`。`actual_cost`、A6 Seller 打款和
真实现金利润继续分别落账；它们用于经营对账，不改变本节的 `P/delta/F` 联合控制规则。

## 3. Provider 加权成本 `K`

### 3.1 选入样本

参与 `K` 的 Pin 必须同时满足：

- 模型与当前定价模型完全相同。
- 渠道不是我方任一商户的渠道。
- Observer 报价在 `quote_max_age` 内，默认不超过 20 秒。
- 至少两个独立 Observer 在最近 30 秒确认相同渠道和价格。
- 渠道不处于不可用、冷却、平台熔断或明确的上游故障状态。
- 价格字段完整，币种和汇率快照存在。

### 3.2 权重

权重表示该 Pin 承担的真实业务单位占比。当前生产口径使用最近一小时的倍率前官方计价等价量：

```text
D_i = 渠道 i 承担请求的“倍率前官方计价”等价量
w_i = D_i / sum(D_j)
K_quote = sum(w_i * Q_i)
```

`D_i` 逐请求按 `prompt_tokens*5,000,000 + completion_tokens*30,000,000 +
cache_read_tokens*500,000 + cache_write_tokens*6,250,000` 汇总；probe 不进入权重。全部 Pin 在窗口内均无
业务量时才等权回退。窗口衰减只能作为后续独立版本引入，不能与当前口径混写。

这里的 `D_i` 应使用倍率前的官方计价等价量或等价 token mix，不得直接把实际支出
`usage_quota` 当权重再乘一次渠道价格，否则昂贵渠道会被重复加权。

若只能读取实际支出 `C_i` 和渠道原始倍率 `Q_i`，先反解：

```text
D_i = C_i / (Q_i / c)
```

冷启动没有用量时，使用计划调度占比；计划占比也不存在时才使用等权。单个 Pin 的计划权重
默认不超过 50%，孤立低价渠道默认不超过 20%。

### 3.3 实付回读与安全成本

上游原始倍率、预计实付和真实实付必须分别保存：

```text
K_quote    = 当前 Pin 的加权上游原始价值倍率
C_quote    = K_quote / c
C_realized = 最近 15 分钟已匹配 Provider 请求的实际支出 / 倍率前官方计价等价量
C_safe     = max(C_quote, C_realized) * (1 + h_uncertainty)
K_safe     = c * C_safe
```

`h_uncertainty` 默认 1%，样本充足且报价与实付连续一致时可降到 0；样本不足、汇率陈旧或成本
方差过大时提高。若同一个 Seller Key 的请求可能落到多个 Provider，使用该路由集合的加权
P90 成本进一步抬高 `C_safe`。最佳实现是按 Key/路由绑定分别计算 `C_safe,j`，避免用全局均价
掩盖单个 Key 的亏损。

## 4. `delta` 运行时合同

### 4.1 能力与读回

每个 Key/模型维护以下独立状态：

```text
delta_capability_verified
delta_write_readback_verified
delta_billing_sample_verified
```

`delta_capability_verified` 表示 Admin API 明确声明范围为 `[0,0.5]`；
`delta_write_readback_verified` 表示本次写入后 GET 返回值与目标值完全一致；
`delta_billing_sample_verified` 表示抽样请求满足 `rate_multiplier=1+delta` 和
`actual_cost=total_cost*(1+delta)`。第三项持续监控实现语义，但不因为 managed Key 使用无限工作
额度而把所有定价目标强制归零。

### 4.2 定价使用的有效 `delta`

规划阶段：

```text
delta_effective = delta_target
```

执行阶段必须先把 `delta_target` 写入并读回；读回成功后该值成为 `delta_effective`，随后才允许
写入依赖它的 Listing 价格。若写入、读回或能力检查失败：

- 不写入更低的 `P`。
- 保持上一个已确认的 `(P,delta)` 组合。
- 进入 `delta_readback_failed` 对账；需要恢复时使用 `delta=0, P>=1.2C_safe`，无不确定性
  示例中等价于 `P>=1.44K`。

实际经营仍持续计算：

```text
F_realized = confirmed_external_cash_revenue / official_cost_equivalent
realized_markup = F_realized / C_realized - 1
```

`F_realized` 是经营结果，`F_expected=(1+delta_effective)P` 是定价合同结果；面板必须同时展示，
不得再用一个字段替代另一个字段。

## 5. 核心定价公式

### 5.1 最低收益目标

```text
F_target = (1 + m) * C_safe
F        = (1 + delta_effective) * P
```

最低收益约束：

```text
F >= F_target
```

给定 `delta_effective`，反解 A6 Seller 最低面板价和最低大厅公开价：

```text
P_profit_floor = F_target / (1 + delta_effective)
L_profit_floor = P_profit_floor / c
```

即：

```text
P_profit_floor = (1 + m) * C_safe / (1 + delta_effective)
L_profit_floor = (1 + m) * C_safe / ((1 + delta_effective) * c)
```

### 5.2 给定市场位置后反解 `delta`

实际控制顺序应为“先选公开市场位置，再反解 `delta`”，避免在两个地方重复乘除：

```text
delta_max = 0.5
```

```text
P_selected = c * L_selected
delta_required = F_target / P_selected - 1
```

应用边界：

```text
if delta_required > delta_max:
    P_selected = F_target / (1 + delta_max)
    L_selected = P_selected / c
    delta = delta_max
elif delta_required < 0:
    delta = 0
else:
    delta = delta_required
```

因此价格从最低档向上升时：

- 最低可行价使用 `delta=0.5`。
- `P` 上升时 `delta` 连续下降。
- 当 `P = F_target = (1+m)C_safe` 时，`delta` 恰好降到 0。
- 继续提高 `P` 后保持 `delta=0`，额外价差全部成为更高利润。

用 `y` 表示最低 Listing 面板价，用 `y1` 表示 delta 衰减到 0 的断点：

```text
y  = F_target / (1 + delta_max) = 0.96K
y1 = F_target                   = 1.44K

P < y                                : 不可行，控制器必须把 P 抬回 y 或停止写价
delta(P) = delta_max                 , P = y
delta(P) = F_target / P - 1          , y < P < y1
delta(P) = 0                         , P >= y1
```

`delta_max` 只是可用倍率上限，不会使 `P<y` 变得可行：此时即便取 `delta=0.5`，仍有
`(1+delta)P<F_target`，会跌破 20% 成本加成率。

若孤立低价保护把最低价从理论 `y` 抬高到 `P_guard`，最低 Listing 不必继续拉满 `0.5`，而是
直接取 `delta=F_target/P_guard-1`。这正是“定到价格 `y`、自动得到 delta `x`、最终利润仍为
20%”的唯一衰减公式。

无不确定性时 `C_safe=K/c=1.2K`。特别地，当 `P=K`：

```text
delta = F_target / P - 1
      = 1.44K / K - 1
      = 0.44
```

### 5.3 `m=20%`、`c=5/6` 的目标模式比例表

| `delta` | `P_profit_floor / K` | `L_profit_floor / K` |
| ---: | ---: | ---: |
| 0.00 | 1.440000 | 1.728000 |
| 0.10 | 1.309091 | 1.570909 |
| 0.20 | 1.200000 | 1.440000 |
| 0.30 | 1.107692 | 1.329231 |
| 0.40 | 1.028571 | 1.234286 |
| 0.44 | 1.000000 | 1.200000 |
| 0.50 | 0.960000 | 1.152000 |

这张表是核心回归测试：`delta=0.44` 时，A6 面板价等于上游原始价值 `K`，最终收益为
`1.44K`，相对实际拿货成本 `C=1.2K` 的利润正好为 20%。`delta=0.5` 时理论最低面板价为
`0.96K`，不是 `0.8K`。

## 6. 给定示例的精确复算

本节按 `delta_effective=delta_configured=0.5`、`h_uncertainty=0` 复算生产目标模式。

三个 Pin 的上游原始价值倍率及权重：

```text
Q = [0.04385, 0.04385, 0.04404]
w = [0.60,    0.20,    0.20]
```

不考虑额外不确定性头寸时：

```text
K = 0.04385*0.60 + 0.04385*0.20 + 0.04404*0.20
  = 0.043888
```

先计算实际拿货成本和 20% 利润目标：

```text
C_landed = K / (5/6) = 0.0526656
F_target = 1.2 * C_landed = 0.06319872
```

若令 `P=K=0.043888`，应反解：

```text
delta = 0.06319872 / 0.043888 - 1 = 0.44
F = 1.44 * 0.043888 = 0.06319872
成本加成率 = F / C_landed - 1 = 20%
```

若使用最大 `delta=0.5`，理论最低 Listing 为：

```text
P_min = 0.06319872 / 1.5 = 0.04213248
L_min = 0.04213248 / (5/6) = 0.050558976
P_min / K = 0.96
L_min / K = 1.152
F_target / K = 1.44
F_target / C_landed = 1.20
```

`P=100, delta=0.2` 时 `F=120`，刚好等于拿货成本 `120`，利润为 0；要达到 20% 利润，
`F` 必须为 `144`，因此 `delta=0.44`。这是生产公式的固定验收反例。

若提供的同一观测快照中 `K_CNY 0.04385` 对应大厅展示 `x0.0594`，则该快照的展示坐标
转换系数为：

```text
g_display_to_k = 0.04385 / 0.0594
               ~= 0.7382154882
R_display_target = 0.050558976 / g_display_to_k
                 ~= 0.0684881
```

面板显示为四位小数时约 `x0.0685`。这是该快照下的展示结果；控制器以 `K_CNY` 的
`P_min=0.04213248`、`L_min=0.050558976` 为写入和验收依据，并从实时 API 重新读取
`g_display_to_k`，不持久化固定的 `0.7382154882` 或 `x0.0685` 作为规则常量。

## 7. 孤立低价与恶意竞价保护

### 7.1 外部市场样本

先排除我方全部 Listing、不可用渠道、陈旧报价和未通过 Observer 共识的结果，再按公开价排序：

```text
Q_(1) <= Q_(2) <= ... <= Q_(n)
```

排序前先按独立外部 `supplier_id` 聚合，每个商户只保留最低的一个健康报价；`Q_(2)` 默认来自
与 top1 不同的外部商户。若 API 暂时缺少 `supplier_id`，才按 `channel_id` 去重，并将保护状态
标记为 `supplier_identity_degraded`，不据此放宽价格底线。

定义：

```text
gap_12 = (Q_(2) - Q_(1)) / Q_(1)
support_1 = 与 Q_(1) 相差不超过 neighbor_ratio 的独立外部商户数量
```

默认：

```text
gap_trigger = 10%
severe_gap_trigger = 25%
neighbor_ratio = 3%
min_support = 2
guard_position = 75%
```

孤立低价判定：

```text
isolated_low =
    gap_12 >= gap_trigger
    or support_1 < min_support
```

`support_1` 包含 top1 本身并按独立 `supplier_id` 去重；默认 `min_support=2`，即 top1 至少要有
一个 3% 邻域内的独立同价位商户。`severe_gap_trigger` 用于提高保护位置；上述 OR 触发关系
始终保持。

有效独立商户少于两个时没有可计算的 top2：冻结所有向下改价，沿用最近一次仍在 TTL 内的
`L_guard`；历史保护价也已过期时，只使用 `L_profit_floor_at_delta_max` 并标记
`market_guard_insufficient_evidence`，等待 Observer 补足样本后再恢复市场跟价。

### 7.2 保护价

触发保护后，不追随 `Q_(1)`，而是在 top1 和 top2 之间取更靠近 top2 的位置：

```text
L_guard = Q_(1) + guard_position * (Q_(2) - Q_(1))
```

默认 `guard_position=0.75`；当 `gap_12 >= severe_gap_trigger` 时提高到 `0.85`。最终低价档
公开底线：

```text
L_profit_floor_at_delta_max = F_target / ((1 + delta_max) * c)
L_hard_floor = max(L_profit_floor_at_delta_max, L_guard)
```

然后用第 5.2 节从 `L_hard_floor` 反解 `P` 和 `delta`。保护价抬高 `P` 后，`delta` 会自然
从 0.5 向 0 收缩，不需要另写一套衰减公式。

对 Pin 侧同时执行：孤立 top1 最多承接 20% 计划流量；其健康或共识不足时直接移出候选集，
其余 Provider 分配给 top2 及其相邻健康渠道，避免 `K` 被单个异常报价支配。

## 8. Listing 价格区间分布

### 8.1 区间边界

```text
lower = L_hard_floor
upper = min(configured_public_cap, observed_market_cap)
```

`configured_public_cap` 是面板可调参数。试点阶段应使用克制的上限，不把 Listing 放到市场
极端最高倍率；高价尾部默认只保留一个探索位置。

### 8.2 按市场密度分配 Seller 数量

在 `[lower, upper]` 上使用对数坐标划分 `B=8` 个桶。桶 `b` 的外部健康渠道数为 `M_b`。
先给有市场样本且处于经营范围的桶分配覆盖位，再按平滑密度分配剩余 Seller：

```text
density_weight_b = (M_b + beta) ^ alpha
alpha = 0.75
beta  = 0.5
```

剩余名额按最大余数法分配：

```text
raw_b = remaining * density_weight_b / sum(density_weight)
```

约束：

- 低价密集区获得更多 Listing，但同一价格点不堆叠多个我方渠道。
- 有真实调用的中间区间至少保留一个覆盖位。
- 空白大区间只放少量探索位，不为了“看起来覆盖完整”浪费大量 Seller。
- 高价尾部默认最多一个探索位。
- 每个目标价加入确定性的最小 tick 偏移，保证可复现且不突破利润底线。

### 8.3 价格与 `delta` 的联合形状

对每个目标公开价 `L_j`：

```text
P_j = c * L_j
delta_j = clamp(F_target,j / P_j - 1, 0, delta_max)
F_j = (1 + delta_j) * P_j
```

目标模式的低价密集区表现为高 `delta`，价格上升时 `delta` 逐步下降，达到 20% 收益且
`P_j >= 1.2C_safe` 后 `delta=0`；无不确定性时该断点为 `1.44K`。更高位置通过提高 `P_j` 获得更高利润，而不是把
`delta` 反向拉高。

## 9. 动态 Pin 输入契约

### 9.1 Observer 调度

- 一个 Observer 周期最长 10 秒。
- 三个 Observer 平分周期，例如相位约为 `0s / 3.33s / 6.67s`，不在同一秒集中抓取。
- 每轮结果立即写入时间序列和当前快照；当前快照用于 Pin，时间序列用于成功率、延迟和价格
  趋势。
- 每个候选必须携带真实渠道 ID、真实名称、价格、成功率、P50、TTFT、标签、样本数、
  Observer 数和观测时间。

### 9.2 评分

默认权重：

```text
[price, responsible_success, latency] = [0.60, 0.15, 0.25]
```

先做硬门槛，再评分。三个分量都映射到 `[0,1]`，分数越高越优：

```text
price_score   = 1 - robust_percentile(log(Q_i))
success_score = Beta 平滑后的责任成功率
latency_score = 1 - robust_percentile(log(max(P50, TTFT)))

score = 0.60*price_score + 0.15*success_score + 0.25*latency_score
```

`robust_percentile` 使用分位数或 Winsorize，不得使用容易被单个离群值拉坏的普通 min-max。

责任成功率只把 Provider、渠道和平台责任失败计入失败；用户请求格式错误、上下文非法、用户主动
取消等客户端责任错误从分母和分子中剔除。样本不足时使用 Beta 先验，避免一次成功或失败得到
0%/100% 的极端判断。

硬门槛默认包括：

- 排除我方渠道。
- 报价和健康数据未过期。
- 不在冷却、熔断或不可用状态。
- 责任成功率低于 80% 且有效样本不少于 20 时暂停候选资格。
- `max(P50, TTFT)` 超过 20 秒且有效样本不少于 20 时暂停候选资格。
- `no_available_channel`、账户额度不足、Pin 漂移和确定的上游 5xx 立即进入紧急路径。

权重以后可以按已确认的“每小时现金利润”做受约束优化，但任何自动调权都必须保留：价格权重
不低于 0.5、三项权重和为 1、单次调整不超过 0.05，并保留前后版本和回放结果。

### 9.3 单飞异步调度与切换

- 任一真实请求进入时，以 `(provider_token, model)` 为键触发异步 Pin 调度器；请求线程只投递
  信号，不等待 Pin 完成。已有调度器运行时合并信号并读取最新 Observer 快照，不重复启动。
- 常态切换使用最短持有 60 秒和最小综合评分改善阈值 5%，防止报价微抖造成反复 Pin：

  ```text
  switch_gain = (score_new - score_current) / max(abs(score_current), 1e-9)
  switch only if switch_gain >= 0.05
  ```

  价格上涨、硬错误、红黄状态和 Pin 回读漂移进入紧急路径，不受 60 秒与 5% 限制。
- 当前 Pin 变红、变黄、不可用、发生硬错误或 Pin 回读漂移时绕过持有期，立即选择下一健康
  候选。
- Pin 写入后必须读取 `/api/marketplace/pins` 确认模型、Token、渠道和回退开关，再恢复调度。
- 每个 Provider 使用独立出口 IP；调度优先级不超过 2，并发根据健康 Provider 容量调整。

### 9.4 紧急 3-5 次请求

紧急切 Pin 后，可执行 3-5 次带随机正常长度的请求，但需满足：

- 独立 Booster Token、预算和审计 ID。
- Callback 规则已从公开生产入口验证命中。
- Pin 前保存原状态，Pin 后回读，请求结束后恢复并再次回读。
- 长度分布包含短、中、长三档，但设定输入 token 和输出 token 硬上限，禁止再次出现超大上下文。
- Provider 可用容量不足、Callback 校验失败、预算耗尽或恢复状态不明确时停止该轮。

抖加的直接回流扣除平台抽成后天然小于支出。经营判定使用：

```text
boost_net_lift = 抖加后外部现金利润增量 - 对照组利润变化 - boost_net_loss
```

只有连续窗口 `boost_net_lift > 0` 才扩大频率；直接回流和内部 managed Key 扣费不计作外部
利润。

## 10. 写入顺序、频率与故障回退

### 10.1 安全写入顺序

跨 Enodot `delta` 和 A6 Listing 的更新采用 Saga，并选择任何中间状态都不跌破利润底线的顺序：

- 降低 `P` 且需要提高 `delta`：先提高并读回 `delta`，再降低并读回 `P`。
- 提高 `P` 且需要降低 `delta`：先提高并读回 `P`，再降低并读回 `delta`。
- Provider 成本突然上涨但 `p2 < 2*p1`：立即先抬高 `P` 到安全价；达到两倍阈值时改走第 10.3 节的
  下架冷静，不在线写入跳涨价。
- Provider 成本下降：连续两轮 Observer 确认并满足降价滞回后再降价。

每一步都有独立幂等键、前置状态、目标状态、读回状态和补偿动作。响应超时但远端状态未知时只
进入 `unknown` 对账，不重复盲写。

### 10.2 Pin 更新触发

Pin generation 成功更新后立即排队 Seller price-band reconcile；10 秒 timer 只作兜底。每一轮必须用
当前所有有效 Pin 的真实业务权重重新计算 `a`，并为全部受控 Listing 重新生成目标价，不能只更新发生
Pin 切换的 Provider 账号。

### 10.3 Pin 成本跳涨的 Listing 硬冷静规则

对每个受控 Listing 独立计算，并按同一商户的全部受控 Listing 组成风险组：

```text
p1 = 该 managed_uid 最近一次成功完成 apply_seller_price_band 的面板倍率
p2 = 当前混合 Pin 加权成本生成的目标面板倍率
threshold = 2
```

规则是：

1. 没有 `p1` 的新 Listing 正常执行首次定价；已有 `p1` 且 `p2 < 2*p1` 时正常改价。
2. 任一 Listing 满足 `p2 >= 2*p1` 时立即创建 durable cooldown，并同步下架该商户的全部受控
   Listing；冷静时长必须落在闭区间 `[30min,45min]`。等于两倍也触发，不能用 `>` 放过边界。
3. 冷静期间冻结最初的 `p1` baseline。后续更高成本只更新最新 `p2`，不得把 baseline 抬高，也不得把
   尚未写入的高价记录成 completed price-band action。
4. 冷静期内新 Pin 使 `p2 < 2*p1` 时立即进入 `release_pending(cost_recovered)`；到达本次确定性
   `cooling_until` 时进入 `release_pending(cooldown_elapsed)`。若成本恢复产生的 release 尚未执行，Pin 又在
   原冷静期限内反弹到 `p2 >= 2*p1`，状态必须退回 cooling，继续保持下架。
5. release 对风险组内每个受控 Listing 固定执行：写入并读回安全 `delta` ->
   `update_listing(expected_status=0)` -> 严格读回目标四项价格且 `status=0` -> `resubmit_listing` ->
   严格读回在线状态。禁止先上架旧低价再更新。
6. release 任一步失败时保持 `release_pending`；价格读回未确认时绝不 resubmit。进程重启后若发现远端已
   以目标价在线，可用严格目标价读回完成恢复；若在线但仍是旧价，先重新下架，再走离线改价顺序。
7. 普通 hysteresis 和 minimum-update-interval 不得阻止新 cooldown 的立即下架或成本恢复后的 release；
   同批其他普通 Listing 可以保持 hold。

生产 Seller pilot 的理论面板价和最终倍率 cap 均为 `10`，再乘 `0.75` band utilization；因此 `4.x` Pin
成本仍能先生成理论安全 `p2` 并进入上述硬规则，而不会在冷静判断之前被旧 `0.4` cap 拒绝。

### 10.4 普通更新频率

- 成本上涨、利润跌破底线、硬故障：下一控制轮立即处理。
- 价格下降：至少 2% 改善且连续两轮确认。
- 普通价格更新最短间隔 30 秒。
- 价格区间职责重排最短间隔 10 分钟；低流量位置延长到 30 分钟以上。
- Listing 上下架错峰执行；改价独立运行，不因错峰上下架而暂停。

### 10.5 数据不足时的安全回退

按以下顺序降级：

1. 使用最近一次仍在 TTL 内的 `C_safe`。
2. 使用健康候选的加权 P90 成本。
3. delta 能力或读回失败时，不执行降价；恢复目标使用 `delta=0, P >= (1+m)C_safe`。
4. 连安全成本也不存在时冻结降价，只允许维持或抬价，并标记 `pricing_data_insufficient`。

## 11. 面板要求

### 11.1 表格名称与模型筛选

- `市场 K_CNY 区间` 改为 `市场实时价格分布（K_CNY）`。
- `受管 Listing 位置` 改为 `受管 Listing 定价与经营`。
- 两张表都提供模型筛选器。试点阶段只有 `gpt-5.6-sol`，仍保留筛选字段和 URL 参数，避免
  后续多模型改表结构。

### 11.2 市场实时价格分布

至少显示：

- K_CNY 区间、外部健康渠道数、我方 Listing 数、最近 10 秒/1 分钟调用频率。
- top1、top2、价差、top1 邻居数、孤立低价保护是否触发及保护价。
- 当前 `K_quote`、`C_quote`、`C_realized`、`C_safe`、汇率和观测时间。

### 11.3 受管 Listing 定价与经营

折叠行主视图只放关键字段：

- 模型、商户、我方 channel ID、Listing ID、真实渠道名称。
- 当前 `K_CNY`、`P`、大厅公开价 `L`、公开倍率、`delta`、预期 `F` 和实收 `F_realized`。
- 成本加成率、收入毛利率、价格档位、控制状态。
- A6 平台真实责任成功率、`max(P50, TTFT)`、样本数、数据年龄和原始标签。

展开后显示：

- 当前 Pin 列表、各 Pin 的真实名称、渠道 ID、价格、权重、成功率、延迟、标签和出口。
- 四项价格向量、取整前后值、公式版本、delta 能力与写入读回状态。
- 最近写入动作、前后读回、错误分类、抖加成本与净提升。

## 12. 持久化与审计字段

每次定价决策至少保存：

```text
decision_id
formula_version
model
currency
pricing_coordinate
source_quote_coordinate
coordinate_version
pricing_basis_version
fx_snapshot_id
official_price_vector
conversion_factor
display_to_k_factor
margin_target
customer_billing_class
delta_configured
delta_effective
delta_capability_verified
delta_write_readback_verified
delta_billing_sample_verified
pin_candidates
selected_pins
pin_weights
K_quote
K_safe
C_quote
C_realized
C_safe
market_top1
market_top2
market_gap
market_neighbor_count
isolated_low_guard
L_guard
L_selected
P_selected
F_expected
rounded_price_vector
expected_markup_on_cost
expected_gross_margin
observed_before
observed_after
action_status
error_class
price_jump_baseline_multiplier
price_jump_trigger_multiplier
price_jump_latest_target_multiplier
price_jump_threshold_ratio
price_jump_state
price_jump_started_at
price_jump_cooling_until
price_jump_release_reason
created_at
verified_at
```

报价、健康和流量事实按时间序列保留；当前状态使用单独的 current 表。结构化数值字段不得只塞在
自由文本日志中。

## 13. 伪代码

```text
quotes = observer_consensus(model, max_age=20s)
eligible = hard_filter(quotes, exclude_all_owned_channels=True)
pins = choose_top_k_by_robust_score(eligible, weights=[0.60, 0.15, 0.25])

pin_weights = demand_equivalent_ewma(pins, window=1h, half_life=15m)
K_quote = weighted_cost(pins, pin_weights)
C_quote = K_quote / conversion_factor
C_realized = matched_realized_cost(window=15m)
C_safe = conservative_cost(C_quote, C_realized, uncertainty)
K_safe = conversion_factor * C_safe

delta_max = 0.5
F_target = (1 + margin_target) * C_safe

market = external_market_only(quotes)
guard = isolated_low_guard(market.top1, market.top2, market.neighbors)
L_min_by_delta = F_target / ((1 + delta_max) * conversion_factor)
L_hard_floor = max(L_min_by_delta, guard.public_floor)

targets = allocate_by_log_market_density(
    listings,
    lower=L_hard_floor,
    upper=configured_public_cap,
)

for target in targets:
    L = target.public_price
    P = conversion_factor * L
    delta = clamp(F_target / P - 1, 0, delta_max)
    if delta == delta_max and (1 + delta) * P < F_target:
        P = ceil_to_tick(F_target / (1 + delta))
        L = P / conversion_factor

    assert (1 + delta) * P >= F_target
    if target.last_completed_P is not None and P >= 2 * target.last_completed_P:
        persist_cooldown(target, baseline=target.last_completed_P, latest_target=P)
        takedown_with_readback(target)
        continue
    if target.cooldown_active and P < 2 * target.cooldown_baseline_P:
        update_offline_with_readback_then_resubmit(target, P, delta)
        complete_cooldown(target, reason="cost_recovered")
        continue
    apply_saga_with_readback(target, P, delta)
```

## 14. 验收标准

### 14.1 固定示例

输入：

```text
Q = [0.04385, 0.04385, 0.04404]
w = [0.6, 0.2, 0.2]
c = 5/6
m = 0.2
delta = 0.5
```

必须得到：

```text
K = 0.043888
C_landed = 0.0526656
F_target = 0.06319872
P_target = 0.04213248
L_target = 0.050558976
P/K = 0.96
L/K = 1.152
F/K = 1.44
F/C_landed = 1.2
```

### 14.2 性质测试

- 对任意合法 `K>0`、`m>=0`、`delta in [0,0.5]`，公式结果满足
  `(1+delta)*P_floor >= (1+m)*(K/c)`。
- `delta` 增大时 `P_floor` 和 `L_floor` 单调不增。
- `P` 上升时反解出的 `delta` 单调不增，并在 `P=(1+m)*(K/c)` 时等于 0。
- micros 取整后利润仍不低于底线。
- 将 `P` 与 `L` 互相换算两次，误差不超过一个价格 tick。
- 输入、输出和缓存四项在真实 token mix 下均满足底线。
- 任意原始报价先归一到同一 `K_CNY` 坐标；`K_USD`、`K_CNY`、`P`、`L` 和页面 `x...`
  之间的每次换算都有独立版本与快照，`c` 只出现在 `P <-> L` 换算中。

### 14.3 反例测试

- `delta` PATCH 成功但 GET 读回不一致时，不写入更低 Listing 价格。
- `p2 == 2*p1` 时立即下架并生成 30-45 分钟 cooldown；`p2 < 2*p1` 不触发。
- cooling 期间 baseline 始终是原 `p1`；新 Pin 成本恢复时提前 release，恢复前再次反弹则退回 cooling。
- release 的调用顺序必须为离线改价、严格读回、最后上架；离线改价读回失败时上架调用数必须为 0。
- 理论最低价低于 `0.96K` 时钳制到 `0.96K`，并保持 `delta=0.5`。
- 市场保护把最低面板价抬到 `(0.96K,1.44K)` 内时，反解部分 delta，且最终倍率仍为 `1.44K`。
- 面板价达到或超过 `1.44K` 后，delta 必须为 0；继续抬价不得再次增加 delta。
- 当 `P=K` 时 delta 必须为 `0.44`；若得到 `0.20`，说明实现把原始倍率 `K` 错当成了
  实际拿货成本 `K/c`。
- top1 与 top2 价差、独立近邻支撑任一命中保护阈值时，最低公开价等于或高于 `L_guard`。
- 独立外部商户不足两个时冻结向下改价，不把单一报价当作可跟随市场底价。
- top1 属于我方渠道时，在计算 top1、K、Pin 候选和市场密度前全部排除。
- 客户端责任错误不降低渠道责任成功率；Provider/平台责任错误会降低。
- Observer 数据过期、Pin 回读不一致或跨系统写入结果未知时，不执行降价。
- 成本上涨并同时需要改 `P`、`delta` 时，任何中间状态都满足利润底线。

### 14.4 生产启用门槛

生产联合定价需要连续完成：

1. Enodot `delta` 能力、写入和读回测试。
2. 倍率后用量日志测试，确认 `rate_multiplier=1+delta`。
3. 影子计划验证 `P`、`delta` 单调关系和每条 `F>=1.2*(K/c)=1.44K`。
4. 按“先 delta 后降价、先涨价后降 delta”的顺序完成单 Listing Saga 测试。
5. 逐步放量：1 个 Listing、3 个 Listing、全部试点 Listing，每级保留前后读回和补偿点。
6. 实际现金利润继续逐请求匹配，并作为独立经营指标告警，不反向把全体 delta 强制归零。

当前生产公式为 `P_min=(1+m)(K/c)/(1+delta_max)`，随后在 `[P_min,F_target]` 内按本章
公式连续衰减 delta。
