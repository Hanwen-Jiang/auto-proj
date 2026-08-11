# A6 / Enodot 买卖系统：接管诊断与重新设计

日期：2026-07-26（UTC 11:15 快照）
适用部署：`us_cloud` → `/opt/a6-trader/current` → `20260726T1100Z-proxy-lease-import`

本文取代 `docs/a6-trading-system-design.md` 中与「增长引擎」和「利润口径」相关的部分。
原设计文档的领域模型、数据表和定价公式仍然有效，不需要推翻。

---

## 0. 一页总结

### 0.1 五个实测结论

| # | 结论 | 证据 |
| :--- | :--- | :--- |
| 1 | **抖加是纯亏损**，净烧 $4.51 且零可观测提升 | 毛支出 $12.30，回流 $7.79，比值恒为 A6 抽成后的 0.833 |
| 2 | **增长引擎已全量关闭且不可能自愈** | `callback_enabled=false` 覆盖 16/16；关闭者是每 60s 运行的退休流程，无任何代码重开 |
| 3 | **退休切断了 Provider 的全部收入** | 10 个已退休商户 live listing 全部为 0，只剩一次性 $1 余额 |
| 4 | **利润不可测量**，面板的 $152.56 无意义 | 归属覆盖率仅 23%；收入里含 $7.79 自有资金回流 |
| 5 | **Pin 的自有渠道排除从未失效** | `own_pin = 0`，2028 条「自买」全部是抖加的有意行为 |

### 0.2 根因一句话

**所有闸门都是单向的，没有组件负责把期望状态断言回去。** 六个闸门
（`callback_enabled`、增长 `enabled`、booster `enabled`、Provider 可用性、价格上限、
pilot timer）全部只有「关」的边。叠加完全缺失效果度量层，所以无效动作只能人工全量关闭，
然后再也没打开。

### 0.3 真实经营口径（修正后）

| 科目 | 金额 |
| :--- | ---: |
| 真实外部 Seller 收益 | **$31.29** |
| 抖加净损耗 | **−$4.51** |
| 已真实出金 | **$0.00**（`paid_micros = 0`，从未提现） |
| 面板原口径「确认利润」 | ~~$152.56~~ 应显示 `null`（覆盖率 23%） |

外部客户实际只有 Enodot 的 user 11（+$40.86）和 user 17（**−$13.12，在亏本服务**）。

### 0.4 交付状态

| 阶段 | 状态 |
| :--- | :--- |
| P0-A 账本真相 | ✅ 代码 + 测试 + migration 只读事务验证通过 |
| P0-B 停止衰减 | ✅ 三个单向闸门全部改可逆 |
| P0-C Provider 存活 | ✅ 新命令 `trade-topup-providers-once` |
| **部署** | ❌ **未部署**，线上一行未改 |
| P1 增长引擎 | ⬜ 设计完成（§4.2），待 P0 稳定后开放 |

654 tests 全绿，`ruff check src/ tests/` clean。

---

## 1. 结论先行

**功能不缺，缺的是闭环。** 30201 行代码已经实现了健康分类、Pin 控制、定价带、抖加、
上下架回收、退休、提现账户、余额划转和面板。问题是：**每一个安全闸门只有「关」的边，
没有「开」的边**，因此系统单调衰减到静止。

更严重的是：**抖加（callback burst）在 04:00–08:00 实际运行过，它是纯亏损的，
并且污染了全部收益指标。** 关掉它之后，真实收益反而更高。

因此接管后的第一件事不是做增长，而是**先把账本口径修正到能区分「自己的钱转圈」和
「外部真实收入」**。在这之前所有增长决策都是盲的。

---

## 2. 实测诊断

### 2.1 抖加是纯亏损，且制造了假收益（最重要发现）

按小时对齐 booster token 支出与 A6 收益：

| UTC 小时 | 抖加支出 (quota) | 全部收益 (quota) | 净额 | 收益/支出 |
| ---: | ---: | ---: | ---: | ---: |
| 04:00 | 433,438 | 365,275 | −68,163 | 0.843 |
| 05:00 | 1,290,602 | 1,094,346 | −196,256 | 0.848 |
| 06:00 | 2,423,851 | 2,026,275 | −397,576 | 0.836 |
| 07:00 | 444,573 | 370,304 | −74,269 | 0.833 |
| 08:00 | 59,276 | 50,246 | −9,030 | 0.848 |
| 09:00 | 0 | 41,321 | **+41,321** | — |
| 10:00 | 0 | 221,450 | **+221,450** | — |

关键读数：

- 抖加运行期间，收益/支出稳定在 **0.833–0.848**，正好是 A6 的
  `seller_earned / buyer_market_quota ≈ 0.83333`。也就是说**那几个小时的收益几乎全部是
  我们自己的钱扣掉 16.67% 平台抽成后回流**，外部收入贡献接近于零。
- 07:30:49 全部 booster token 被置为 `enabled=false` 之后，10:00 小时出现
  **221,450 quota 零成本收益**，高于抖加期间任何一小时的净额。

按 `request_id` 双向匹配后的精确口径（migration 0021 在只读事务中实测）：

| 科目 | quota | USD |
| :--- | ---: | ---: |
| 抖加毛支出 | 6,152,145 | $12.30 |
| 抖加回流（`own_boost`） | 3,897,294 | $7.79 |
| **抖加净损耗** | **2,254,851** | **$4.51** |
| 真实外部收益（`external`） | 15,644,133 | $31.29 |
| Pin 错配自买（`own_pin`） | 0 | $0 |

30,681 条收益事件中 1,919 条（6.3%）是抖加回流，**全部来自 11 个 `callback` booster
token；`own_pin` 为 0**，说明 Pin 的自有渠道反连接从未失效，之前看到的「自买」全部是
抖加的有意行为，不是事故。

因此原面板把 $7.79 的自有资金回流当作收入计入，同时没有扣除 $12.30 的抖加支出 ——
单这一项就让 P&L 虚高约 **$12.30**。

结论：抖加的**概念**没错（A6 路由确实偏好近期成功、低错误率的渠道），但**实现没有度量**，
所以无法区分「自买回流」和「真实拉新」，最终变成用 16.67% 的固定折价买入假指标。

同时它解释了另外三个现象：

- `a6_provider_usage_events` 中 2028/7099 条命中我们自己的渠道 —— 这不是 Pin 错配事故，
  而是抖加的自买流量（token_ref 与 `a6_channel_booster_tokens` 完全对应，
  最后一条 10:32:37 对齐 booster 7011 的 `last_used_at` 10:32:34）。
- 收益曲线好看，但 `actual_profit_micros = null`。
- 成本侧 6014 条事件里 4652 条无法归属（$12.93 / $16.11）。

### 2.1.1 退休把 Provider 的收入来源直接切断

除了角色变更，退休流程还调用 `takedown_all_listings` **下架该商户的全部 Listing**。实测：

```text
10 个已退休商户，live listing 数全部为 0
```

所以退休后的 Provider 只剩一次性划入的约 $1 用户余额、零收入、零可提现，花完即永久
跌破 $1 门槛。这是 2.3 里 Provider 集体饿死的**机制性死因**，比角色问题更致命。

### 2.2 增长引擎已被全量关闭，且不可能自愈

`a6_channel_growth_policies`（16 行）实测：

| enabled | recycle_enabled | callback_enabled | 行数 |
| :--- | :--- | :--- | ---: |
| f | t | f | 9 |
| t | t | f | 4 |
| t | f | f | 2 |
| f | f | f | 1 |

- `callback_enabled = false` 覆盖 **16/16**：抖加通道彻底关闭。
- `enabled = false` 覆盖 **10/16**。
- 历史上确实跑过：`callback_burst` completed 41 / failed 16，
  `listing_recycle` completed 40 / failed 1，`callback_readiness` failed 6。

关闭源头是**退休流程**，它是单向的：

- `src/a6api_auto/trade_seller_retirement.py:756` —
  `SET enabled = false, callback_enabled = false`
- `migrations/0020_retired_provider_observer.sql:44` — 同样的批量关闭

而 `a6-trader-seller-retirement.timer` **每 60 秒**执行一次。任何被退休的商户，其增长策略
被永久关闭，**代码里没有任何路径会重新置为 true**。因此增长能力随时间单调趋零。

健康探测本身是好的（`trade_channel_health.py` 分类完备，2797 行 observer 健康记录：
degraded 1030 / healthy 748 / cooling 618 / idle 201 / unlabeled 152 / unavailable 48）。
**探测到了，但动作被闸门锁死** —— 这正是「为什么没有加以相应处理」的答案。

### 2.3 Provider 全体饿死

`--min-observers 2 --top-k 5` 的 Pin 运行结果里，12 个 Provider 有 **10 个**被排除，
原因统一为 `provider_user_balance_below_one_usd`，只剩 2 个活跃 Pin。

| merchant | user_balance (quota) | ≈USD | withdrawable | lifetime_earned |
| ---: | ---: | ---: | ---: | ---: |
| 3 | 501,871 | $1.00 | 4,064 | 506,846 |
| 6 | 395,336 | $0.79 | 0 | 501,446 |
| 9 | 102,886 | $0.21 | 0 | 500,924 |
| 10 | 101,089 | $0.20 | 0 | 552,856 |
| 11 | 19 | $0.00 | 0 | 534,067 |
| 12 | 98,142 | $0.20 | 0 | 504,197 |
| 13 | 3 | $0.00 | 0 | 501,031 |
| 15 | 53,041 | $0.11 | 0 | 620,746 |
| 17 | 9,598 | $0.02 | 0 | 515,619 |
| 18 | 2,523 | $0.01 | 0 | 763,897 |

机制性死亡：每个 Provider 累计只赚到约 $1.0–1.5（作为 Seller），转入用户余额后作为
Provider 花光，`withdrawable` 归零，跌破 $1 门槛即被永久排除。
`transfer_in_count = 10`（历史总计），**没有自动补充回路**。

叠加放大这个问题的是：退休把它们从 Seller 身份移除，等于**切断了它们唯一的收入来源**。
10 个 Provider 各需锁定 $1 缓冲，共 $10，而系统当前真实外部收益约 $0.44/小时 ——
provider 数量严重过配。

### 2.4 利润不可测量

`GET /api/overview?window=all`：

```
earned_micros                       39,020,112   ($39.02)
enodot_sales_micros                119,578,916   ($119.58, 602 req)
owner_internal_debit_micros        180,743,940   ($180.74, 784 req)  ← 已排除
provider_cost_micros                16,113,270   ($16.11, 6014 事件)
  confirmed                          3,178,338   ($3.18)
  unattributed                      12,934,932   ($12.93, 4652 事件)  ← 77% 无法归属
confirmed_profit_micros            152,557,316   ($152.56)
actual_profit_micros                      null
profit_status                        "partial"
paid_micros                                  0   ← 从未真实出金
```

`confirmed_profit = $152.56` 这个数字**不能作为经营依据**：

1. 它把「Enodot 全站销售额」和「A6 套利收益」相加，而 Enodot 的外部流量大部分并不由
   我们的 A6 Provider 承载（这也是 77% 成本无法归属的原因）。
2. 收入侧 $39.02 的 A6 收益中，04:00–08:00 段几乎全是抖加自买回流。
3. `paid_micros = 0`：没有任何一分钱真实提现落地。

真实外部客户只有 Enodot 的 user 11 和 user 17（近 7 天）：

| user | 请求 | 计费 total_cost | 上游 actual_cost | 毛利 |
| ---: | ---: | ---: | ---: | ---: |
| 11 | 785 | $62.85 | $21.99 | **+$40.86** |
| 17 | 174 | $26.24 | $39.36 | **−$13.12** |

**user 17 在亏本服务**，说明按 Key 的 delta / 定价没有覆盖其实际路由成本。

### 2.5 工程治理

- `git log` → **0 次提交**；86 个未跟踪文件；`main` 分支无任何历史。
- 根目录 **70+ 个 `dist-*` 时间戳目录**，一天内 19 个 release。
- 没有 diff、没有回滚、没有 bisect。这正是「退休关闭增长」这种死策略能长期存活而
  无人发现的直接原因。
- 所有关键参数（`--margin 0.20`、`--maximum-price-multiplier 0.4`、
  `--top-k 5`、`--probe-budget 8`）硬写在 systemd 单元参数里，调参需要发新 release。

### 2.6 其它

- `--maximum-price-multiplier 0.4` × `--maximum-band-utilization 0.75` → 实际价格上限
  **0.30**，而目标是 1.0。merchant 31 已经贴在 0.300 上限。
- `a6-trader-seller-pilot.timer` 状态 `inactive dead` —— 实时重定价循环未被调度。
- `a6-trader-pin.timer` 每 10 秒触发，单次运行 >8 秒，且 pin 服务**未加 flock**
  （growth / pilot 都加了），存在重叠执行风险。
- `no_available_channel` 分类已实现（`trade_errors.py:89`）并在
  `trade_pin_runner.py:695` 使用，这一条符合要求。

---

## 3. 根因

一句话：**所有闸门都是单向的，没有任何组件负责「把期望状态重新断言回去」。**

| 闸门 | 关闭者 | 重新打开者 |
| :--- | :--- | :--- |
| `callback_enabled` | 退休流程（每 60s） | **无** |
| `enabled`（增长） | 退休流程 | **无** |
| booster `enabled` | 手工 / 失败累积 | **无** |
| Provider 可用性 | 余额 < $1 | **无**（无自动补款） |
| 价格上限 | 硬编码 0.4×0.75 | 需发版 |
| seller-pilot 调度 | timer 被停 | **无** |

同时缺少一个**效果度量层**：抖加、回收、定价档位都没有「做了之后外部收益有没有变好」的
对照，所以无法自动停掉无效动作，只能靠人手工全量关闭 —— 然后就再也没打开。

---

## 4. 修正后的设计

### 4.1 第一优先：账本真相层（一切的前提）

**在 ingest 时就给每条事件打上「资金来源」标签**，而不是事后 join 推断。

`a6_earning_events` 增加：

```sql
counterparty_class text NOT NULL DEFAULT 'unknown'
  -- 'external'  : 外部买家（唯一计入收入）
  -- 'own_boost' : 我方 booster token 自买
  -- 'own_pin'   : 我方 provider token（Pin 错配，事故）
  -- 'unknown'   : 未能判定
counterparty_token_ref_id bigint
```

判定依据：`a6_earning_events.request_id` ⋈ `a6_provider_usage_events.request_id`。
两侧我们都有完整记录，因此判定是确定性的，不需要时间窗模糊匹配。

派生口径（面板只显示这三个）：

```text
external_seller_income   = Σ earned WHERE counterparty_class='external'
circular_gross_loss      = Σ (own_boost 支出 − own_boost 回流)
attributable_provider_cost = Σ provider_cost WHERE 已唯一匹配 Enodot 请求

honest_profit = external_seller_income
              + external_enodot_revenue
              − attributable_provider_cost
              − circular_gross_loss
              − withdrawal_fee
```

硬规则：

- **`attribution_coverage < 0.9` 时，面板显示 `null` 并标注覆盖率，绝不显示一个数字。**
  当前覆盖率是 23%，所以现在就该显示 null（它确实显示了 null —— 这个行为是对的，
  要保留；错的是同时显示了 `confirmed_profit = $152.56`，应移除该字段）。
- `paid_micros` 与 `withdrawable` 分列展示，永不相加。已实现，保留。

回填：对 04:00–08:00 的历史数据执行一次幂等回填，把抖加回流打上 `own_boost`。
这会让历史收益曲线大幅下修 —— 这是**正确的**，当前曲线是假的。

### 4.2 增长引擎：健康 → 动作 → 度量 → 自动开关

核心改动：**每个动作都必须能被证明有效，否则自动停止；而「停止」是带 TTL 的，会自动重试。**

#### 4.2.1 状态机

复用现有 `classify_channel_health` 的七态输出，动作映射：

| 健康态 | 动作 | 前置条件 |
| :--- | :--- | :--- |
| `cooling` / `unavailable` | `listing_recycle`（下架 → 0.8–2.0s → 上架） | observer 法定数 ≥2 一致；该渠道 5 分钟内无在途请求 |
| `degraded` / `unlabeled` | `callback_burst`（抖加） | 见 4.2.3 的准入门槛 |
| `idle` | `callback_burst`，低预算 | 同上，且预算减半 |
| `healthy` | 无 | — |

#### 4.2.2 闸门必须双向

替换所有 `SET enabled=false` 为**带过期时间的抑制**：

```sql
ALTER TABLE a6_trade.a6_channel_growth_policies
  ADD COLUMN suppressed_until timestamptz,
  ADD COLUMN suppression_reason text,
  ADD COLUMN suppression_count int NOT NULL DEFAULT 0;
```

- 失败 / 退休 / 无效 → 写 `suppressed_until = now() + backoff(suppression_count)`，
  指数退避，上限 6 小时。
- **不再写 `enabled=false`**。`enabled` 只由人在面板上操作。
- 退休流程改为：只调整 duty，**不触碰增长策略**（见 4.4）。
- 一个 `growth-reconciler` 每分钟把过期的抑制清零，并把 `enabled` 重新断言为期望值。

#### 4.2.3 抖加的准入门槛与预算（针对 2.1 的亏损）

抖加的唯一合法目的是**让 A6 路由把外部买家导向我们**。因此：

准入（全部满足才发）：

1. 健康态 ∈ {degraded, unlabeled, idle}，且 observer 法定数 ≥2 一致。
2. 该渠道**过去 24h 有过 `external` 收益**（证明它能接到真实买家）。
   没有外部收益历史的渠道禁止抖加 —— 这一条直接排除了 04:00–08:00 那种全量盲打。
3. 该渠道当前面板价在可盈利区间（`final ≥ 1.2K`）。
4. 预算未超。

预算（三层硬上限，全部以 USD 计，面板可调）：

```text
per_channel_hourly    ≤ $0.05
per_merchant_hourly   ≤ $0.20
global_hourly         ≤ $0.50
global_daily          ≤ $3.00
```

参考量级：单次探活成本 ~$0.000033（1085 次 pin probe 共 $0.036），
`-c 15 -n 85` 的短路请求在 `max_output_tokens=16` 下同一量级。
对比 2.1 的实际燃烧速率（06:00 小时净亏 $0.80），上述全局上限把最坏情况压到 1/10。

**效果度量（这是之前完全缺失的部分）：**

```sql
CREATE TABLE a6_trade.a6_growth_action_outcomes (
  action_id bigint PRIMARY KEY REFERENCES a6_trade.a6_channel_growth_actions(id),
  health_before text NOT NULL,
  health_after_60s text,
  health_after_900s text,
  external_earned_before_900s bigint,   -- 动作前 15 分钟的外部收益
  external_earned_after_900s bigint,    -- 动作后 15 分钟的外部收益
  control_external_earned_after_900s bigint,  -- 同档位未动作渠道的对照
  gross_cost_micros bigint NOT NULL,
  net_lift_micros bigint,               -- (after − before) − control_delta − cost
  evaluated_at timestamptz
);
```

自动开关规则：

- 每个 `(health_state, action_type)` 组合独立统计最近 30 次的 `net_lift_micros` 中位数。
- 中位数 ≤ 0 → 该组合进入 `suppressed_until = now() + 6h`，并在面板告警。
- 中位数 > 0 → 允许，并按 lift 大小分配预算份额。
- 样本 < 10 时按「探索预算」运行，上限为正常预算的 20%。

**这条规则如果早就存在，04:00 那一小时结束后抖加就会被自动停掉，
不会连续烧 4 小时。**

#### 4.2.4 上下架回收

- 仅 `cooling` / `unavailable`。
- 单渠道冷却：同一渠道 ≥10 分钟一次，指数退避。
- 下架前检查在途请求；有在途则跳过（避免打断真实买家）。
- 下架 → `random(0.8, 2.0)s` → 上架，全程写意图 → 执行 → 回读确认。
- 度量：`health_after_60s` 是否脱离 cooling。恢复率连续低于 30% 则抑制。
- 不采用 `post_recovery_load_started`（按要求）。

#### 4.2.5 短路与错误分类

保持现有 `no_available_channel`（503）与 `account_quota_insufficient`（403）分类，
补齐一条：**分类命中后必须触发后台诊断链** `balance → transfer_policy → pin`，
并把诊断结论写入 `trade_incidents`。客户端仍只见
`provider_pool_temporarily_unavailable`。

### 4.3 Provider 流动性与选举（回答「20% 怎么定」）

**20% 不应作为固定比例。约束不是容量，是流动性自足。**

```text
required_providers = max(
    2,
    ceil(peak_concurrency / (safe_concurrency_per_provider * 0.7)),   # 容量
    ceil(hourly_provider_spend / hourly_self_funding_per_merchant)    # 流动性
)
```

按当前实测（外部收益 ~$0.44/h，provider 总支出 $16.11 累计）：
**目标 provider 数 = 3，不是 10。**

理由：每个 provider 需锁定 ≥$1 才可用，10 个 provider 锁定 $10 并把收益摊薄到
各自 ~$1 累计，必然全部跌破门槛；3 个 provider 各自能拿到更大份额的自有收益，
可以自足。

三条硬规则：

1. **Provider 必须保留 Seller 身份**（`{seller, provider}`）。
   退休把 provider 变成 `{provider, observer}` 等于切断其收入来源 —— 这是 2.3 的
   机制性死因，必须改。
2. **自动补款回路**：`user_balance < $2` 时，从自身 `withdrawable` 划转补到 $2。
   - 走 Preview → 以返回为准 → 意图落库 → 执行 → 回读确认（现有机制已具备）。
   - 最小 $1（= 一个 `quota_per_unit`），3 秒限频。
   - `withdrawable` 不足时**不排除该 provider**，而是降低其调度优先级并告警，
     等待结算到账（A6 收益 → withdrawable 有结算延迟，因此必须预留缓冲而非事后反应）。
3. **周期重选**，而非永久退休。评分：
   `0.4×liquidity_runway + 0.2×session_health + 0.2×proxy_health + 0.2×recent_success`。
   每 30 分钟重选，切换不改变 Seller 上架状态。

Enodot 侧调度参数按 Pin 倍率加权（现有逻辑保留）：优先级上限 2，并发按实时情况调整。

### 4.4 退休流程的修正

现状：退休 = 移除 Seller 身份 + 永久关闭增长。两者都错。

改为：

- 退休只表示「不再参与定价带分配」，`roles` 保持含 `seller`。
- **不触碰** `a6_channel_growth_policies`。
- 已有的 A6 Listing 保持在售（实测退休商户仍在产生 $0.4–1.4/日收益，这是净收入，
  没有理由关掉）。
- 删除 `trade_seller_retirement.py:756` 的批量关闭，新增 migration 把
  0020 造成的 `callback_enabled=false` / `enabled=false` 复位。

### 4.5 定价

- 价格上限从 `0.4 × 0.75 = 0.30` 提到 **1.0**（分两步：先 0.6 观察 24h，再 1.0）。
- 公式保持不变，已验证正确：
  `F = (1+delta)·P`，`K_seller = P/c`，`P_min = 1.2K/(1+delta)`，
  `delta=0.5 → P = 0.8K`，公开价 `0.96K`，`F = 1.20K`。
- 针对 2.4 的 user 17 亏损：**按 Key 的 delta 必须覆盖该 Key 实际路由成本**，
  而不是只覆盖 A6 市场成本 K。增加每 Key 的 `realized_cost_multiplier` 监控，
  连续 1 小时 `billed < actual_cost` 则自动上调该 Key 的 delta 或降优先级。
- 恢复 `a6-trader-seller-pilot.timer` 调度。

### 4.6 数据量与持久化（回答「日志会不会太占空间」）

实测依据：

| 表 | 行数 | 体积 | 每行 |
| :--- | ---: | ---: | ---: |
| `public.usage_logs` | 76k | 76 MB | ~1 KB |
| `public.ops_system_logs` | 250k | 594 MB | ~2.4 KB |

结论：**结构化逐请求事件很便宜，自由文本运行日志才是空间杀手。**

策略：

- 逐请求事件（`a6_earning_events`、`a6_provider_usage_events`）**永久保留** ——
  它们就是 P&L 总账，是唯一能算清 4.1 口径的东西。19 商户 × 当前流量 ≈ 2–5 MB/日。
- 财务快照**只在变化点 + 整点 + 动作前后**插入（现有设计已正确），
  最坏 19 × 288/日 ≈ 5.5k 行/日 ≈ 1–2 MB/日。
- 5 分钟 / 小时 / 日三级 rollup 供面板查询，避免每次扫原始表。
- 市场报价只存 Top-N（N=10）+ 全部自有渠道；原始响应仅在哈希变化或异常时留 7 天。
- 自由文本日志：已有 `journald` 200 MB 上限与 PG 日志轮转，保持。

**不需要为了省空间牺牲逐请求事实。** 相反，2.4 的问题恰恰是事实不够细。

### 4.7 工程治理（必须先做，否则 4.1–4.6 会重演）

1. `git init` 已有仓库但零提交 → 立即建立基线提交，`.gitignore` 排除 `dist-*`、
   `.venv`、`secrets`。
2. 清理 70+ 个 `dist-*` 目录，改为单一发布路径 + git tag。
3. 所有运行参数从 systemd 单元参数迁移到**数据库配置表 + 面板可调**，
   带 `config_version` 审计。发版不再是调参的前提。
4. `a6-trader-pin.service` 补 `flock`，与 growth / pilot 共用或独立锁。
5. Pin timer 从 10s 放宽到 30s（单次运行 >8s，10s 触发必然重叠）。

---

## 5. 落地顺序

严格按此顺序，每一步都有可验证的验收标准。

### P0-A 账本真相（0.5 天）

- `counterparty_class` 字段 + ingest 时判定 + 历史回填。
- 面板移除 `confirmed_profit`，改为 `honest_profit` + `attribution_coverage`。
- 验收：04:00–08:00 的收益被正确标记为 `own_boost`；
  `external_seller_income` 曲线与 09:00 之后的零成本收益一致。

### P0-B 停止衰减（0.5 天）

- 退休流程不再关闭增长（4.4）+ migration 复位 16 行策略。
- `suppressed_until` 替代 `enabled=false`（4.2.2）。
- `growth-reconciler` 每分钟断言期望状态。
- 验收：24 小时后 `callback_enabled=false` 的行数不再单调增长。

### P0-C Provider 存活（0.5 天）

- provider 缩到 3 个，保留 `{seller, provider}` 双身份。
- 自动补款回路，floor $2。
- 验收：连续 24h 无 `provider_user_balance_below_one_usd` 排除。

### P1-A 增长度量层（1 天）

- `a6_growth_action_outcomes` + 对照组 + net_lift 计算。
- **先只观测，不放开动作。**
- 验收：能对历史 81 次已完成动作回算出 net_lift。

### P1-B 放开回收（0.5 天）

- `listing_recycle` 在 cooling/unavailable 上启用，带在途检查与退避。
- 验收：恢复率 > 30%，且 net_lift 中位数 > 0。

### P1-C 放开抖加（0.5 天）

- 严格按 4.2.3 的四条准入 + 三层预算 + 自动停止。
- 从 global_hourly $0.10 起，观察 6 小时再逐步放到 $0.50。
- 验收：net_lift 中位数 > 0；否则自动抑制并保持关闭。

### P2 定价与治理（1 天）

- 价格上限 0.30 → 0.6 → 1.0。
- 按 Key 成本覆盖监控（修 user 17 亏损）。
- git 基线、dist 清理、参数入库、flock、timer 调整。

---

## 6. P0 实现状态（2026-07-26 已完成，未部署）

代码与测试已完成并全绿（654 → 665 tests, ruff clean）。**尚未部署到 `us_cloud`。**

### P0-A 账本真相 ✅

| 变更 | 位置 |
| :--- | :--- |
| `counterparty_class` / `counterparty_token_ref_id` / `classified_at` + 索引 + 历史回填 | `migrations/0021_earning_counterparty_class.sql` |
| 增量分类器（按 `request_id` 双向匹配，带结算窗口） | `trade_repository.classify_earning_counterparties` |
| 每个采集轮次末尾调用 + `own_pin_leak_detected` 输出 | `cli.trade_collect_once_command` |
| 收益按来源拆分；`seller_income` 只含 `external` | `_MERCHANT_QUERY` 的 `earning_flow` / `boost_flow` |
| `confirmed_profit` → `honest_profit` + `attribution_coverage`，覆盖率 < 0.9 返回 `null` | `trade_repository` / `trade_dashboard` |

`_coverage` 三边计价：未归属销售、未匹配成本、未分类收益都算缺口。当前实测覆盖率 23%，
因此面板会正确显示 `null` 而不是 $152.56。

migration 在只读事务中验证通过：`UPDATE 30681`，分类结果与 2.1 表格一致。

### P0-B 停止衰减 ✅

三个单向闸门全部改为可逆：

1. `trade_seller_retirement.py` 不再写 `SET enabled = false, callback_enabled = false`。
2. 角色从 `('provider','observer')` 改为 `('seller','provider')`，保留收入能力。
3. **不再调用 `takedown_all_listings`** —— Listing 保持在售。
4. 增长目标查询移除 `AND retirement.completed_at IS NULL`，改为
   `suppressed_until IS NULL OR suppressed_until <= now()`。

`suppressed_until` / `suppression_reason` / `suppression_count` 已建列；migration 只把
0020 造成的 10 行标记为 `retirement_legacy_disable`，**不自动重开任何动作** ——
放开 recycle / callback 属于 P1-B / P1-C 的显式决策。

### P0-C Provider 存活 ✅（代码）

新增 `src/a6api_auto/trade_provider_topup.py` 与
`a6api-auto trade-topup-providers-once`：

- 目标余额 `--floor-units`（默认 2 个 `quota_per_unit` = $2，留 1 单位余量）。
- 缺口按整单位向上取整，避免一次划转后仍贴在门槛下。
- `withdrawable` 不足时报 `awaiting_settlement` 并保留职责，**不排除该 Provider**。
- 复用 `a6_balance_transfer_intents` / `a6_balance_transfer_confirmations`：
  意图 → 执行 → 回读确认；在途意图只回读对账，**永不重发同额划转**。
- 默认 dry-run，`--apply` 才动钱。

仍待人工执行：**把 Provider 收敛到 3 个**（走已有的 `trade-assign-roles --apply`，
带预览与审计），以及给已退休商户重新上架 Listing。

## 7. 保留不动的部分

以下已实现且正确，不要重写：

- `classify_channel_health` 的七态分类与证据留存（`trade_channel_health.py`）。
- 定价公式与 `segmented_log_density_v1` 档位分布（`trade_seller_price_band.py`）。
- Collector 的水位增量同步、固定代理映射校验、`upstream_mutations: false` 契约。
- Pin 候选的自有渠道反连接、Top-K、observer 法定数。
- 意图 → 执行 → 回读确认三段式与 `idempotency_key` 审计。
- 秘密处理：会话/Key/Admin Key 不入库、`0600`/`0700` 权限、面板不接触凭据。
- `no_available_channel` / `account_quota_insufficient` 分类与统一对外错误。
- 提现账户池的预留 → 创建 → 稳定 Remark 回读。

---

## 8. 决策记录（2026-07-26 已确认）

| 议题 | 决定 | 影响 |
| :--- | :--- | :--- |
| 抖加 | **带度量重做，预算从 $0.10/h 起** | 保留概念，四条准入 + 三层预算 + `net_lift` 自动抑制（§4.2.3）。先只观测，能对历史 81 次动作回算 lift 后再放开 |
| Provider | **收敛到 3 个 + 保留 Seller 身份 + 自动补款** | 退休不再移除 `seller`、不再下架 Listing；新增 `trade-topup-providers-once` |
| 起步范围 | **先做 P0 三项** | 账本真相 / 停止衰减 / Provider 存活。不放开任何新的花钱动作 |

仍待确认：

1. **价格上限推进节奏**：当前实际上限 `0.4 × 0.75 = 0.30`，目标 1.0。
   建议 0.30 → 0.6 → 1.0，每档观察 24h。
2. **历史收益下修**：回填后面板 Seller 收益从 $39.08 降到 $31.29。这是正确的修正
   —— 差额 $7.79 是自有资金回流，之前被错记为收入。

---

## 9. 部署计划

**当前线上一行未改。** migration 只在只读事务中验证过（`UPDATE 30681` 后 `ROLLBACK`）。

### 9.1 部署前置条件

仓库 `main` 分支**零 commit**，没有任何回滚点。部署前必须先建立 git 基线，否则这次改动
和上次「退休关闭增长」一样无法追溯或回退。

### 9.2 顺序

```bash
# 1. git 基线（前置条件，不可跳过）

# 2. 新 release 目录并安装
ssh us_cloud
sudo install -d -o root -g root /opt/a6-trader/releases/<新版本>
# 同步源码后：
/opt/a6-trader/venv/bin/pip install -e '.[trade]'

# 3. 应用 migration 0021（幂等，含历史回填）
/opt/a6-trader/current/venv/bin/a6api-auto trade-migrate

# 4. 切换 current 符号链接，重启受影响服务
sudo systemctl restart a6-trader-collector.timer
sudo systemctl restart a6-trader-channel-growth.timer
sudo systemctl restart a6-trader-seller-retirement.timer
sudo systemctl restart a6-trader-dashboard.service

# 5. 补款先 dry-run，确认计划后才 --apply
/opt/a6-trader/current/venv/bin/a6api-auto trade-topup-providers-once \
  --merchant 5 --merchant 7 --merchant 19 \
  --proxy-map-file /var/lib/a6-trader/secrets/proxy-map.json \
  --floor-units 2
```

### 9.3 验收标准

| 检查项 | 期望 |
| :--- | :--- |
| 收益分类完成 | `counterparty_class` 无 `unknown`（超出 15 分钟结算窗口的部分） |
| `own_pin` 泄漏 | collector 输出 `own_pin_leak_detected: false` |
| 外部收益口径 | `external_earned_micros` ≈ 15,644,133 quota（$31.29） |
| 利润不再虚报 | `honest_profit_micros` 为 `null`，`attribution_coverage` ≈ 0.23 |
| 衰减停止 | 24h 后 `callback_enabled=false` 的行数**不再单调增长** |
| Listing 不再被下架 | 已退休商户 live listing 数 > 0 |
| Provider 存活 | 连续 24h 无 `provider_user_balance_below_one_usd` 排除 |

### 9.4 仍需人工执行

1. **Provider 收敛到 3 个** —— 走已有的 `trade-assign-roles --apply`（带预览与审计），
   不要用 SQL 直接改 `roles`，会绕过审计链。
2. **给已退休商户重新上架 Listing** —— 它们当前 live listing 为 0，光有 `seller`
   角色还不产生收入。
3. **价格上限提升** 与 **按 Key 成本覆盖监控**（修 user 17 亏本服务），见 §4.5。

### 9.5 已单独立项

- git 基线 + 清理 70+ 个 `dist-*` 目录 + 修 `make lint`（vendored 目录 14 个 ruff 错误）。
- `a6-trader-pin.service` 补 `flock` 并把 10s timer 放宽（单次运行 >8s，必然重叠）。
