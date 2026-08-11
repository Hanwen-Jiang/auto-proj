# Listing 自动上下架与利润门禁语义（2026-08-04 确认版）

> 状态：用户确认口径。本文件是 listing-schedulability duty、Channel 利润门禁表和
> 后续修改的唯一语义来源。与代码或面板文案冲突时以本文为准。
> 适用模型：`gpt-5.6-sol`（试点）。

## 1. 价格坐标与单位（不可再混用）

- **K_USD**：相对于官方标准价格（`B_t`，见 §3）的倍率 `P`，经平台换算后的
  **大厅公开倍率 `L = P / c`**（`c = 5/6`，即面板价↔大厅价坐标换算）。
- **K_CNY**：`K_USD × 汇率`。
- **realtime_ratio_cny 是计算值，不是独立观测**：
  ```text
  P   = listing_price_micros / B_t
  L   = P / c = P × 1.2                 （c = 5/6）
  realtime_ratio_usd = L                 （市场行情成对上报）
  realtime_ratio_cny = L × 汇率
  ```
  例（listing 4822）：`P = 235,116/30,000,000 = 0.0078372` →
  `L = 0.0094046`（= realtime_ratio_usd）→ `K_CNY = 0.0635`（×6.7526）。
  即：**listing 自身的价格在 K_CNY 坐标下就是 realtime_ratio_cny**，
  二者是同一价格的两种坐标，不存在"listing 价 ≠ 市场价"的分离。
- **汇率**：使用市场配对汇率（`avg(realtime_ratio_cny / realtime_ratio_usd)`，
  实测约 `6.7526`），禁止硬编码 `7.2`。无新鲜行情时才回退到
  `ECONOMIC_FX_CNY_PER_USD`。
- **所有门禁比较都在 K_CNY 坐标**：channel 的 `realtime_ratio_cny`（即该 listing
  价格的 K_CNY 值）与账号门禁 `k × gross_multiplier`（同为 K_CNY）直接比较。

## 2. sub2api 账号成本 k_CNY（45933 示例：周成本 ¥20）

计算顺序（**有真实数据优先，无数据才模拟**）：

1. 有可用 quota 快照（`used_a + remaining_a` 均非空且和 > 0）：
   `k = cycle_cost_cny / (used_a + remaining_a)`
   例：`20 / (17.76 + 159.8) = 0.1126`（2026-08-04 16:23 UTC 快照）。
2. 无可用快照（账号 0% 用量、没有其他可观测值）：
   `k = cycle_cost_cny / 100`（**100A 是模拟用的临时周配额，不是真实数据**）。
   例：`20 / 100 = 0.20`。
3. `declared_k` 模式：直接用 `declared_k_cny`。

**结论：有快照时 duty 的 `0.1126` 是正确的；`0.20` 只是无数据时的模拟值。**

## 3. 官方基准价 `B_t`（`gpt-5.6-sol`，per 1M tokens）

```text
(input, output, cache_read, cache_write) = (5_000_000, 30_000_000, 500_000, 6_250_000) micros
listing_price_micros_t = ceil(B_t × P)
```

## 4. 利润门禁（配置驱动，禁止硬编码 1.6 / 1.92 / 1.44）

- 利润率 bps：来自运行时配置 `a6_trade.model_profit_settings.minimum_profit_on_cost_bps`
  （当前 6000 → 60%）。
- `net_multiplier = 1 + bps/10000`（6000 → **1.6**）——净结算门槛系数。
- `gross_listing_multiplier = net_multiplier × A6_GROSS_LISTING_FACTOR`
  （1.6 × 1.2 = **1.92**，其中 1.2 = `1/c`，`c=5/6` 面板价↔大厅价坐标换算，
  不是另一笔费用）——Listing 价格门槛系数。
- Provider 侧净利润：来自 active `provider_cost_generations.margin_bps`
  （当前 2000 → 20%）；无该值时的回退参数 `--provider-profit-percent`（默认 20）。
  `provider_gate_multiplier = (1 + margin/10000) × 1.2`（20% → **1.44**）。

## 5. 账号可调用（callable）定义

sub2api 账号可调用需同时满足：

1. `health_state = HEALTHY`；
2. `capacity_state ∈ {AVAILABLE, THROTTLED}`；
3. 剩余配额 `remaining_a ≥ minimum_remaining_a`（默认 **20A**，过滤近耗尽账号）；
4. `source_k_cny()` 可计算（§2）。

Provider 可调用 = `provider_inventory.schedulable = true` 且状态非
RETIRED/QUARANTINED。

## 6. Listing 上/下架判据（reconcile_a6_listing_schedulability.py）

对每个受管 listing 独立决策（幂等、seller lock、远端读回、审计）：

1. **merchant 自身 provider 可调度** → 该 merchant 的 listing 上架
   （reason `provider_schedulable`）。
2. **无任何可调用源**（sub2api 与 provider 都不可调用）→ 下架
   （reason `no_callable_source`）。
3. **Provider 路径**：有可调用 provider 且
   channel 的 `realtime_ratio_cny ≥ provider_cost × provider_gate_multiplier` →
   上架（reason `provider_profit_gate_passed`）。
   provider_cost = active cost generation 的 `realized_cost_p95_multiplier × 市场fx`。
4. **sub2api 路径**：取**最便宜可调用账号**（k 最小），
   channel 的 `realtime_ratio_cny`（= 该 listing 价格的 K_CNY 坐标值）≥
   `k × gross_listing_multiplier` → 上架（reason `sub2api_price_gate_passed`）。
   例：45933 最新快照 k=0.0987 → 门禁 ¥0.1895；listing 5823 的 K_CNY
   = 3.8895 ≥ 门禁 → 上架；listing 4822 的 K_CNY = 0.0635 < 门禁 → 下架。
5. 已上架且满足门禁 → keep；已上架但不满足 → 下架。
6. **无新鲜市场报价（realtime_ratio_cny）的渠道一律下架**
   （reason `no_market_quote`）——无法证明其 K_CNY 过门禁。

**净结算（channel revenue）不是上/下架条件**：它是经济路由资格
（eligibility，`sub2api_channel_source_eligibility`）的输入，
决定"哪些渠道会被请求面路由"，与 listing 是否在架无关。
（此前把净结算作为上架条件导致无流量→无收入→永远不上架的循环，已废弃。）

## 7. Ban 与上/下架的关系（平台行为实证）

- **Ban 必须在上架状态执行**：provider 侧 `marketplace_channels`
  （`route_status=all` + `include_user_disabled` + `exclude_unavailable=false`）
  只返回**在架** listing 的渠道；下架 listing 不在 inventory 中，
  `_verify_known_owned_visible` 无法校验、`marketplace_disable_channel` 无法执行。
- 实证：listing 5092（channel 1149）下架时 inventory 无 1149；上架后立即出现。
- 正确流程：**上架 → Ban（disable 设 `user_channel_disabled=true`）→ 最终落为
  下架 + ban 标记**。暴露窗口只在同一次操作内。
- auto-pool 的 `_prepare_account` 应在 Ban 前对 owned 渠道做 seller 侧
  resubmit（若已下架）。
- listing-schedulability duty 的 relist 应**跳过已 ban
  （`user_channel_disabled=true`）的渠道**，避免 resubmit 清除 ban 标记。

## 8. Channel 利润门禁表（dashboard 列语义）

| 列 | 语义 | 取值 |
| --- | --- | --- |
| 净结算 K_CNY | 渠道近期实收折合的 K_CNY（usage 计费 × 市场 fx） | `channel_net_k_cny` |
| 最低源站 K_CNY | **可调用**（ELIGIBLE/THROTTLED）源站中最低的 k | 只统计可调用源 |
| 60% 利润门槛 | 最低可调用源站的净门槛 `k × net_multiplier` | 只统计可调用源 |
| Headroom | 净结算 − 门槛（及 bps） | 同上 |
| 可用源站 | 可调用且门槛被过的源站列表 | 空 = 无可服务源 |

修复记录：`minimum_source_k_cny` / `minimum_required_channel_k_cny` 的聚合
必须 `FILTER (WHERE eligibility_state IN ('ELIGIBLE','THROTTLED'))`，
否则 EXHAUSTED 源的 0 会把门槛显示成 ¥0.00。

## 9. 审计与幂等

- 每次变更写入 `trade_control_actions`（action_type `schedulability_listing_action`，
  idempotency_key `schedulability:{model}:{merchant}:{listing}`）+
  `trade_control_action_attempts`，远端读回确认（status=2 上架 / status=0 下架）。
- 全程持有 `/var/lib/a6-trader/seller-control.lock`（fcntl，unit 不再外层 flock）。
- 静默快路径：全 keep 时零网络开销。
- duty 输出 JSON 含 `serving_account_ids`（预留）、gate 来源
  （`model_profit_settings` / `default`）、市场 fx、provider cost。

## 10. 配置入口汇总

| 配置 | 位置 | 当前值 |
| --- | --- | --- |
| sub2api 利润率 bps | `a6_trade.model_profit_settings` | 6000 → 1.6 / 1.92 |
| provider 利润率 | active `provider_cost_generations.margin_bps` | 2000 → 1.44 |
| provider 利润率回退 | `--provider-profit-percent` | 20 |
| 可调用最小余量 | `--minimum-remaining-a` | 20 |
| fx 回退 | `--fx-cny-per-usd` | 7.2（仅在无行情时） |
| 账号观测窗口 | `_ACCOUNT_EVIDENCE_MAX_AGE_SECONDS` | 90s |
| revenue 窗口 | `--revenue-max-age-seconds` | 600s |
