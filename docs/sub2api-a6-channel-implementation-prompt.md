# Sub2API v0.1.173 原生 A6 Channel 经营模块实现 Prompt

> 当前精简执行版 v15，2026-08-11

请在当前定制版 Sub2API v0.1.173 中实现原生 A6 Channel 经营模块。主体逻辑从
`/Users/haven/Documents/code/a6api-auto/archive/legacy-trader` 的 Trader 迁移并重新梳理，保留现有 Managed Seller、
Class2Adjustment、A6 Provider、Pin、Auto-Pool、Replay 和生产升级兼容能力。

当前 v0.1.173 已包含并上线 `Class2Adjustment`。它是本实现必须保持兼容的既有能力，不是待重新迁移的
`delta` 实现。经营 `delta` 与 `Class2Adjustment` 共享最终 usage compositor，但拥有独立的策略、账本和观测事实。

实现依据：

- Sub2API：`/Users/haven/Documents/code/codex-pool/sub2api-update-20260811-v0173`
- A6 API：`/Users/haven/Documents/code/a6api-auto/docs/a6-api-reference.md`
- A6 抓包：`/Users/haven/Documents/code/a6api-auto/api-docs/7-24-15-54.md`
- Supplier Profile：`/Users/haven/Documents/code/a6api-auto/api-docs/api-supplier-profile.md`
- A6 Status：`/Users/haven/Documents/code/a6api-auto/api-docs/api-status.md`
- 错误分类：`/Users/haven/Documents/code/a6api-auto/errorlist.md`
- 生产约束：`/Users/haven/Documents/code/a6api-auto/AGENTS.md`

## 1. 实施方式

编码前提交并审核：数据模型/约束/Decimal/加密；三层边界图；Listing与Class1状态机；两套Key Saga；A6写入、结果不明、
读回/补偿/重放顺序；旧Seller/Provider/OAuth/API Key迁移映射；每个状态、hold和operation的owner及自动/失败/人工出口。

实现必须包含数据库迁移、后端服务、请求路径硬门、异步控制器、管理端和测试；不得用占位实现替代远端写后读、
经济结算或故障恢复。

## 2. 领域边界

### 2.1 输入/上游部门

负责 Sub2API 从上游取得服务能力：

- Class1 A6 Provider、Class2 原生 OAuth、Class3 上游 API Key。
- `provider_route_token`、Auto-Pool、Pin、Provider liquidity 和 owned-Channel Ban。
- 账号 `sk`、profit、模型能力、健康和 Sub2API 原生调度状态。
- route、cost、capability 和 billing generation。

### 2.2 输出/下游部门

负责通过 A6 Channel 对外提供服务：

- `users.user_type='a6-channel'`、唯一 `output_channel_ingress_key` 和 `ToA6API`。
- 一个本地用户绑定一个远端 A6 Channel；一个 Channel 下绑定多个按模型区分的 Listing。
- 每个 Listing 独立管理 `ck`、delta、价格边界、资格、上下架、冷却和收益核对。

### 2.3 连接层

先以 `(local_user_id, requested_model)` 唯一解析稳定 `listing_binding_id`，再以
`(listing_binding_id, account_id, normalized_model)` 计算 eligibility 和 delta。

每个 attempt 在派发前冻结：

```text
output_channel_generation, listing_generation, account_generation, route_generation,
cost_generation, capability_generation, billing_generation, pricing_generation,
model_price_generation, fx_generation, transport_profile_generation, delta_effective
```

派发前任一 generation 失效都必须重新选择；已经派发的 attempt 按不可变快照结算，新 generation 只阻止新请求。

## 3. A6 Typed Client

1. 控制面统一使用`(a6_user_id, system_access_token)`；一套凭据可绑定多个输出Channel/Listing和输入资源。
2. Typed Client完整覆盖A6 API Reference，包括Token删除、模型/市场/Supplier、Pin/Auto-Pool、circuit、earnings及读回。
3. system token envelope encryption；业务表只存secret reference/fingerprint/last4；所有Token、Key、session、Authorization、
   Cookie在日志/trace/错误/审计摘要中脱敏。
4. 写操作保存操作者、部门、摘要、source generation、幂等键、写前读和写后读。传输结果不明先GET：已生效则adopt，
   仍为旧值则同operation重放，第三种状态按远端事实重规划。
5. 集合读取完整分页并标记completeness；不完整快照的“未找到”不能证明删除、恢复或Ban。
6. 管理端通用控制台只调用白名单；受管写入仍走同一Outbox和单写者。

已确认`PATCH /api/supplier/me/profile`抓包请求体为`contact_info + description`，`GET /api/status`响应合同见上述独立抓包；
Typed Client按这两份协议建模并保留未知扩展字段。当前Profile抓包未出现Supplier `name`，不得拿description或Channel PATCH冒充重命名。

## 4. 核心数据模型

至少建立以下实体，最终字段按第 1 节的数据模型评审落地：

```text
a6_credentials, a6_output_channels, a6_output_channel_instances
a6_output_channel_keys, a6_listing_bindings, a6_listing_instances
provider_route_token_bindings, account_cost_generations
account_economic_profiles, account_capability_generations, a6_model_price_generations
a6_listing_pricing_generations, a6_listing_account_delta_overrides
a6_listing_account_eligibility, a6_output_attempt_economic_snapshots
a6_output_winner_billing_intents, a6_output_revenue_reconciliations
a6_public_text_selections, a6_listing_holds, a6_output_recovery_operations
a6_provider_route_operations, a6_platform_incidents, a6_control_outbox
```

关键约束：

```text
a6_credentials.a6_user_id UNIQUE for active control owner；Token轮换写同一credential的secret generation
a6_output_channels.local_user_id UNIQUE；当前 a6_channel_id 全站 UNIQUE
每个 output_channel 任意时刻最多一个有效 ingress key
(output_channel_id, normalized_model) UNIQUE；当前 a6_listing_id 全站 UNIQUE
provider account 与当前 remote_token_id 均一对一 UNIQUE
(listing_binding_id, account_id, normalized_model) UNIQUE for delta override
economic_snapshot.attempt_id UNIQUE；billing_intent.economic_snapshot_id UNIQUE
revenue_reconciliation.(a6_credential_id, request_id) UNIQUE
每个远端资源任意时刻最多一个 current-generation 写 operation
```

稳定本地 `listing_binding_id` 是 delta、eligibility、hold、attempt、账单和收益的外键。远端 Channel/Listing 删除重建时
关闭旧 instance 并创建新 instance，不改写历史外键。

所有金额、倍率和经济判定使用 NUMERIC/Decimal，向上量化规则必须显式；`float64` 只可作为展示兼容副本。

### 4.1 公共文本语料硬门

所有Supplier名/简介、Channel名/简介、Listing简介和A6/Sub2 Token或Key显示名都必须先从仓库语料选定：Supplier
字段取`resources/corpora/merchant_templates_split.xlsx`“商户资料”同一行；Channel字段及Listing简介分别取“渠道资料”
同一行的`channel_name/channel_description/model_description`；令牌名取`resources/corpora/token_name_corpus.json`。
`nickname`只做所选Channel名的平台长度投影，不拼接ID、模型、职责、类别、时间、hash或后缀。写前持久化catalog SHA-256、
sheet、source row/index、原值和selection generation；重试复用原选择，重名冲突改选未占用语料项。语料不可读、无效、耗尽
或冲突时增加`TEXT_CORPUS_INVALID`并停止对应远端写：输出资源投影Listing hold，输入Token保持账号不可调度。Listing
`display_name`固定为空，直至版本化语料新增专用字段；管理员仅能选择或随机重选语料。迁移旧值保留审计原文，资源进入ACTIVE前完成语料化修正。
既有Supplier name不在语料且确需修改时保持`TEXT_CORPUS_INVALID`；当前Profile抓包只证明简介可写，须取得实际rename合同或明确的远端replacement流程后再解除。

## 5. 输出 Channel 用户

1. 新增`users.user_type='a6-channel'`，不复用role；一用户绑定一远端Channel、一入口Key和多个模型Listing，Key固定同一
   Sub2 UserID，不按模型切用户。
2. 鉴权后以规范化模型唯一解析Listing；只接受A6 `platform-models.model_no`精确值或版本化显式alias，碰撞进入绑定审核。
3. 初始余额0且可持续为负；`concurrency=0`和`quota=0`分别表示无限并发/配额。仅此类型跳过余额、reserve和platform quota，
   Key/IP/RPM/并发/分组/模型/eligibility仍校验。
4. 禁止普通登录、自助注册和用户端Key管理；`users.status`管Channel生命周期，逐Listing admission管模型状态；`/v1/models`
   只暴露当前绑定且允许的模型。
5. 首个、新增及replacement Channel均使用`POST /api/supplier/apply`，以request受理和inventory出现新ID为完成证据；
   replacement切换稳定binding，不把Listing resubmit冒充Channel恢复。

`ToA6API` 是输出入口的唯一 canonical 分组：输出用户只能使用它，普通用户严格排除；禁止改名、删除和误授权；组倍率
及用户组倍率恒为 `1.0`。Class1/2/3 候选由账号 binding 显式投影，经济倍率不得再存进分组倍率。

现有`TPA6API`及其到`ToA6API`的运行时alias只是旧输出入口命名，不是输入部门/第二业务组。迁移前盘点两名称的group ID、
用户、账号和Key：双实体先按Channel binding合并再退役旧组，同一实体旧名则原地canonicalize；验证唯一入口Key和候选投影
后删除alias。此后只精确匹配`ToA6API`，禁止名称fallback。

Channel/Listing 生命周期统一如下：

1. 新建 Channel 时先创建 inactive 用户和 DISABLED/STAGED 唯一 Key；写入远端 Channel、同步完整 Listing inventory、
   完成 owned Ban、价格、能力和 eligibility 后再启用用户和 Key，逐 Listing 恢复。
2. 远端 Listing 删除但父 Channel 与 canonical model 不变时，保留稳定 `listing_binding_id`，关闭旧 instance并创建新
   instance；canonical model改变时退役旧 binding并创建新 binding。
3. Channel 被删除或 takedown 后不猜测原 ID 可恢复；通过 `POST /api/supplier/apply` 申请 replacement，切换新的
   Channel instance，稳定本地用户、Channel binding和历史外键不变。
4. 退役 Listing 时先关闭 admission并等待该 binding inflight归零，再 takedown/readback并关闭 current instance；
   超时由Lifecycle Controller保持`RETIRE_PENDING`、核对stale attempt lease并重试；远端写前可人工取消，写后只能继续或修复
   重放。最后一个Listing退役后再takedown父Channel、删除入口Key并退役用户。
5. owner/model/channel唯一匹配可由 Binding Controller自动采用新 generation；owner冲突、多候选或资源消失进入
   `BINDING_REVIEW`，管理员只可选择指定实例、接受远端事实并生成新 generation，或退役。

## 6. 两类 Key 与轮换

### 6.1 输出入口 Key

`output_channel_ingress_key` 属于输出部门，是 A6 自有 Channel 调用 Sub2API 的本地 Key：

```text
锁定 output Channel
-> 创建 DISABLED/STAGED 新 Key并给全部子 Listing 加 OUTPUT_KEY_ROTATION hold
-> 全部远端 Listing 下架并读回，更新远端 Supplier Channel Key
-> 禁用旧 Key并启用新 Key，任意时刻最多一个有效
-> 精确确认远端新Key配置，清除各Listing的OUTPUT_KEY_ROTATION hold
-> 普通Output Recovery operation分批resubmit
-> 每项保存写后读审计；Marketplace精确可见且circuit未冷却即恢复admission
-> 删除旧本地 Key
```

各中断点都保持全部子 Listing admission=CLOSED。远端仍指向旧 Key时回滚到“旧 Key唯一启用”；远端已确认指向新 Key时
继续到“新 Key唯一启用”。若远端只回显 last4/fingerprint，则结合 Channel配置 generation做精确判定；仍有歧义时保持
下架并进入可重放 operation，禁止同时启用新旧 Key。删除旧 Key失败只进入清理 dead-letter，不回退已经确认的新 Key。

### 6.2 Provider 路由 Token

`provider_route_token` 属于输入部门，是 Class1 请求 A6 上游的远端 Token：

```text
创建新远端 Token
-> 读取旧 Token 的模型、额度和route配置；为新 Token创建新的Auto-Pool subscription或新的Pin assignment
-> 完成 owned-Channel Ban、精确读回及route/cost/model/callability验证
-> 原子切换 account绑定
-> DELETE旧远端 Token，确认详情不存在且列表无旧ID
```

新 Token就绪前旧 Token继续服务，但只有本地当前绑定的 Token可调度。切换前失败时删除新 Token并保留旧绑定；切换后
旧 Token删除失败时将其记录为 `ORPHANED_REMOTE_TOKEN`，持续以同一 operation DELETE/readback，禁止重新绑定或复制出
第二个可调度账号。远端写结果不明统一按第3节先读回再采用、重放或重规划。

两类资源必须使用不同表、字段、权限、服务和 Saga；名称遵循第4.1节，duty只存控制面。Key material规范化为且仅为一个`sk-`前缀。

## 7. 输入账号与 sk

### 7.1 Class1

Class1 是 A6 Provider API Key：

```text
Auto-Pool: sk = 完整纳入价格带的 topN_max_cny_ratio
Pin:       sk = 当前 pin_assignment_epoch 精确 Channel K_CNY
```

- 默认 `target_n=10, min_n=3, max_n=20, min_ratio=0, hard_max_ratio=0.3`。
- 外部可调用比率排序为`r1<=...<=rM`；`M<min_n`时range exhausted，否则`N=min(target_n,M)`、`max_ratio=rN`并完整纳入同价带；
  `cv_n=stddev(r1..rN)/mean(r1..rN)`、`tail_jump=(rN-rN-1)/rN-1`，mean/variance/min只观测。
- 不叠加 `realized_cost_p95`、`retry_reserve` 或额外 `5/6` 换算。
- 成本上涨立即切新 generation；下降连续 3 个快照后生效。
- Pin 不使用 Top-N，成本和模型必须精确匹配当前 assignment epoch。

### 7.2 Class2

Class2 为 Free/Plus/Team/K12/Pro OAuth，以人工管理为主：

```text
multiplier = expected_weekly_quota_multiplier, default 1.0, constraint > 0
used_A > 0 AND utilization > 0: one_week_A = used_A / utilization
otherwise: one_week_A = 100
expected_total_A = one_week_A * multiplier
sk = account_cost_cny / expected_total_A
```

`utilization` 使用 Decimal，可大于 1；只影响成本估算。100% 不暂停、不刷新、不替换、不退役账号，账号状态继续遵循
Sub2API 原生 OAuth 规则。字段缺失或矛盾时保留最近确认 sk；没有历史值时用 `one_week_A=100` 并标记估算。

### 7.3 Class3

- 使用稳定类别 `class3:<provider_code>`。
- 管理员填写账号默认 `declared_sk`，可按规范化模型覆盖。
- 适配器同步 `/v1/models`、余额、Acost 和账单。
- 15 分钟窗口、至少 20 个收费请求且 expected cost >= CNY 1：偏差不超过 5% 正常，5%-15% 告警，
  连续三窗超过 15% 暂停，单窗超过 30% 立即暂停。
- 恢复使用上游账单重同步和有界审计 Probe，连续 3 个窗口不超过 5% 后生成新 billing generation。

默认 profit：Class1 `0.20`、Class2 `0.50`、Class3 `0.40`；允许账号级覆盖，每个 provider_code 有独立默认值。
Class2/Class3 入池前同步模型并对当前 A6 OpenAI 模型做有界探测，生成版本化白名单。
探针原始 input usage范围默认 `500..3000`，全局并发、每分钟请求数和每小时成本上限均可配置；达到预算只延后探测，
不绕过模型能力或账单恢复条件。Class3账单偏差恢复的每个15分钟审计窗口仍需至少20个收费样本且expected cost >= CNY 1；
预算不足时保持暂停并显示原因，管理员可修正 declared sk或扩大审计预算。

## 8. Class1 Auto-Pool 与 Ban

首次启用、Token onboarding、每次路由变更/恢复，以及任一owned Channel创建、采用或恢复时，按Token+模型完整分页读取含
unavailable/disabled/user-disabled/hidden/retired的marketplace，并与全部本地controlled Channel合并owned set。后者必须
使所有可能自路由到该Channel的Provider Token更新到current owned-set generation。先读后写：已全部满足
`user_channel_disabled=true && route_status=user_disabled`时只更新attestation；否则只disable漂移项并逐项读回。Auto-Pool写后
还必须确认`auto_channels`非空且与owned set无交集。

owned Listing仅上架后可见时，使用`BAN_DISCOVERY`：摘所有可能自路由的Class1、保持本地CLOSED，在资源锁内设
`temporary_remote_override=LISTED_FOR_BAN`并由Output Recovery单写者临时上架、Ban/readback、恢复原远端状态后释放override。
它不修改业务desired，也不要求eligible账号；成功、失败、超时、失租或generation替换都必须先恢复/readback。

Auto-Pool上游失败无需证明具体Channel；将当前区间视为失效或集体改价：

```text
暂停受影响 Class1
-> fresh Observer/完整 marketplace snapshot并执行target_n + 1，纳入下一完整价格带
-> 生成新route/cost generation并完成Ban校验
-> PUT Auto-Pool并精确读回
-> 恢复 Class1
```

稳定/距上次变更各30分钟、成功100次、无上游错误，且`cv_n>5%`或`tail_jump>3%`时每轮`target_n-1`。超过`max_n`、
无非owned可调用项或下一完整价带超过0.3时进入`AUTO_POOL_RANGE_EXHAUSTED`并摘流；每60秒fresh重评，重新出现合法完整
价带则生成route/cost generation并按Ban合同恢复。人工可切Pin、退役或重放；禁止截断同价带、空auto_channels或越界。

PUT超时仅摘受影响Class1并进入`ROUTE_UNKNOWN`，GET后adopt、重放或重规划；旧route不再视为远端事实。Provider shadow
默认关闭，仅在必须创建且无可复用generation时单次运行，timer始终关闭。

Pin不执行`target_n+1`或自动切Auto-Pool。当前assignment epoch的Channel运行失败时摘流并创建repin generation；选择新
Channel、Ban、写Pin并读回assignment/channel/cost/model后激活。失败/结果不明保持摘流并可重放；route mode只能人工修改。

Class1 `insufficient_quota` 立即摘流并由Liquidity Controller置`UNFUNDED`，不扩池：先划转merchant可提现整单位并读回；
满足合同则清除此状态；否则移除Provider duty、槽位置VACANT，再引入通过Ban、Observer range、成本、能力和读回的替补。

liquidity/callback Token 与 Provider route Token、输出入口 Key均为独立资源和duty，名称来自令牌语料。它只可Pin到当前
listed且callable的A6自有small-merchant Channel；没有合格Channel时跳过划转请求。生产callback继续使用精确输入
`PING 5db98307ce2d442ee568baacf8b0e7076fa657cbe2806ccff9da6386997915e2`，并验证既有realistic sampled usage与
high-cache不变量；`prompt_tokens=1/completion_tokens=0/total_tokens=1`等probe-shaped结果不计作有效划转。

## 9. ck、官方价格与 FX

`commission=0.2` 固定，`ck` 定义为商户净 `K_CNY`：

```text
ck_public = ck * 1.2
fx_usd_cny = realtime_ratio_cny / realtime_ratio_usd
ck = fx_usd_cny * listing_price_micros[canonical] / official_price_micros[canonical]
```

A6 官方价格来自 `/api/platform-models`。本模块的Supplier Listing统一使用`charge_type=per_token`，按
input、output、cache_read、cache_write中第一个官方价非零维度选择canonical。写入目标ck时，每个非零官方维度：

```text
listing_price_micros[d] = ceil(official_price_micros[d] * ck / fx_usd_cny)
```

写后对每个非零维度计算 `ck_d=fx_usd_cny*listing_price_micros[d]/official_price_micros[d]`。canonical维度用于展示，
经济门使用 `confirmed_ck=min(ck_d)` 作为保守净倍率；各维度只允许由单个micros向上取整产生的误差，超出该整数误差包络
即视为价格向量漂移。批量调用`markup-preview`，按`channel_id`映射`percent/source`。缺项、重复、非法值、
`percent != 20`、billing、官方维度、charge type或价格向量不一致时增加`OUTPUT_CONFIG_INVALID`，修复并读回后恢复。

A6远端读回是ck、markup、Listing状态和模型官方价格的事实源。Sub2API为`ToA6API`建立独立的版本化价格resolver，使用同一
A6官方USD价格向量计算`base_actual_cost`，不全局覆盖普通分组定价。A6暂时不可达时继续使用`last_confirmed_ck`和当前
model-price generation；写入失败进入待重试operation，不覆盖最后确认事实。

FX 优先使用 A6 marketplace 同一快照的 CNY/USD 比值。A6 缺失或不可达时使用：
```text
https://api.frankfurter.app/latest?from=USD&to=CNY
json_path=rates.CNY, timeout=3s, cache_ttl=30m
fallback_active_refresh=15m, max_source_date_age=96h, credential=null
```

外部源只兜底，不与 A6 投票；A6 恢复立即切回。两者都不可用时沿用 `last_confirmed_fx`，不执行新价格写入。

## 10. Listing 定价范围与分布

```text
listing_hard_min_ck default 0.025
listing_hard_max_ck default 4.0
0 < hard_min <= hard_max <= 4.0

effective_delta_cap = min(0.5 if delta_transport_capable(profile) else 0
                          for profile in required_transport_profiles)
class1_floor = min_ready_class1_for_model(sk * (1 + profit) / (1 + effective_delta_cap))
raw_lower = max(hard_min, class1_floor)
band_lower = min(raw_lower, hard_max)
band_upper = hard_max
```

上下限是逐模型可管理policy，但hard max不得配置超过4.0。`ready_class1_for_model`必须满足Sub2原生可调度、模型能力、
当前route/cost generation和current owned-set Ban attestation；瞬时满并发不改变ready。没有ready Class1时只用hard min；
`raw_lower>hard_max` 时价格固定在 hard max，
不得生成超过上限的 Listing，最终是否下架仍由 eligibility 决定。

在 `[band_lower, band_upper]` 建 8 个对数桶。仅采样 A6 外部、可调用、非 owned Channel；公开 k 先除以 1.2 变为净 ck。
按外部市场桶频率用最大余数法分配我方 Listing，桶内用实际分位点；用稳定匹配最小化价格移动。零样本把当前价裁剪到
当前hard bounds后保持，
新 Listing 用 band lower。上下界相等时退化为单点价，不计算对数。分布范围是同一规范化模型下全站受管、未退役的
输出 Listing；等成本时保持稳定 binding顺序。每批默认最多写 10 个，普通改价每 Listing 至少间隔 5 分钟。

价格顺序：

- ck 下调先发布 `min(last_confirmed_ck, desired_ck)` 的保守经济 generation，再等待旧 inflight 清零。普通分布调价超时
  时取消本轮降价并恢复 `last_confirmed_ck` generation，不写远端；当前价突破 `hard_max` 的纠正不得取消，保持 admission
  CLOSED并继续等待/重试，归零后写入上限并读回。
- ck 上调先写远端并读回，再发布允许降低 delta 的 generation。
- 写结果不明时暂停该 Listing 后续调价，GET 后 adopt、重放或重规划。

## 11. Delta、Class2Adjustment、计费与收益

对每个 `(listing_binding_id, account_id, normalized_model)`：

```text
required = sk * (1 + profit)
delta_auto = max(0, required / ck - 1)
delta_effective = COALESCE(delta_override, delta_auto)
requires_delta = delta_effective > 0

structural_eligible =
    model_capable
    AND account_schedulable_by_sub2api
    AND 0 <= delta_effective <= 0.5
    AND ck * (1 + delta_effective) >= required

attempt_eligible =
    structural_eligible
    AND (NOT requires_delta OR delta_transport_capable(request_transport_profile))
```

不得再乘 commission。`delta_override` 可空、永久、无需审批、范围 `[0,0.5]`；清空恢复自动值，且不能绕过公式。
transport profile至少区分Responses/Chat、stream/non-stream和终态usage schema；无需调整的请求保持原usage。每个Listing
generation从已启用的A6入口adapter生成`required_transport_profiles`；只有每个required profile都至少有一个attempt candidate才可清除
`NO_ELIGIBLE`，未暴露profile不参与。数据库保存版本化事实/稀疏eligibility，Redis投影candidate、`requires_delta`、
transport能力和generation；满并发不改变资格。required集合为空或实际收到未声明profile时增加`OUTPUT_CONFIG_INVALID`，
当前请求不处罚账号，修复profile generation后恢复。

### 11.1 经营 delta

经营 delta 是输出部门的 `(Listing, account, model)` 调整，不复用 Class2Adjustment：

1. 每次派发冻结经济和generation快照；换账号新建快照，失败attempt不扣费。
2. 成功终态发出前以CAS选定唯一winner，保存原始/最终usage、两份decision/hash和唯一billing intent；状态为
   `PREPARED|TERMINAL_WRITTEN|DELIVERY_UNKNOWN|BILLED|RECONCILED|VOID`。中断或投递不明由同一`request_id`的A6 earnings
   决定继续Apply还是等待；完整收益/结算同步超过配置终局窗口仍无记录时转VOID并保留tombstone，迟到收益只能在原intent
   上重开并做一次补账。已BILLED后的延迟只记对账异常。
3. 仅winner幂等扣负余额：`ActualCost=BalanceCost=ceil_scale8(base_actual_cost*(1+delta_applied))`，复用现有
   NUMERIC(20,8) UsageBilling；snapshot、claim和Replay共用幂等身份。
4. 确定性solver基于确认的整数price vector，从原始usage求达到
   `ceil(base_merchant_earning*(1+delta_applied)*(1+class2_adjustment_rate))` 的最小合法整数usage，保存目标、实际收益和
   overshoot，并保持endpoint的input/output/cache/total不变量；两项调整都为0时逐字节不变。
5. A6统一根据返回token usage计费，delta通过同一usage改写链路生效。`delta_transport_capable`只表示endpoint adapter
   能够取得并合法改写最终usage。上线验收执行一个`delta_required>0`请求，确认compositor、返回usage、本地
   ActualCost和A6收益均使用同一delta。

两项调整共用改写前`input_tokens`，缺失时取`prompt_tokens`，均含cached input。低于1000时都跳过、保持base cost和原
usage，记录`input_usage_below_1000`及shortfall；这是明确的小额利润例外，不failover或下架。达到1000才调整。
`requires_delta=true`却缺usage/合法schema时禁止发成功终态，并撤销该账号对应transport profile的资格。仅在尚未向A6
发出响应首字节时才换candidate；stream已发出内容后立即终止该流、进入`DELIVERY_UNKNOWN`和earnings核对，不再派发第二
账号，避免重复内容或双结算。

### 11.2 Class2Adjustment

v0.1.173的Class2Adjustment是上线基线：

- 标识/配置使用`Class2Adjustment/class2_adjustment`，API为`/api/v1/admin/settings/class2adjustment`，规则文件为
  `/etc/sub2api/class2adjustment-rules.json`；旧billing adjustment名称只兼容读取，新写使用新名称。
- 仅`account.IsOpenAIOAuth()`命中；保持现有算法、shadow/apply、berserk、probe passthrough和观测，不承担eligibility或delta职责。
- `<1000`跳过，`=1000`生效；不覆盖原UsageLog、component cost和base ActualCost，只通过返回usage传输并记录decision。
- 普通流量仍可单独运行它；只有`a6-channel`流量可能同时命中经营delta。

### 11.3 两套体系的合成

两项策略独立计算，但winner只合成一次：

```text
delta_decision = evaluate_delta(economic_snapshot, U0)
class2_decision = evaluate_class2adjustment(selected_account, U0)
final_usage = compose_once(U0, delta_decision, class2_decision)
```

每份decision独立保存policy/generation、rate、skip reason、原始hash和目标收益，均只读取`U0`。仅delta改变本地
ActualCost；最终因子为`(1+delta_applied)*(1+class2_adjustment_rate)`。compositor、intent和Replay共用幂等键；普通
Class2缺usage沿用既有skip，依赖delta的A6 attempt按11.1节失败。

以 A6 earnings 的 `request_id` 对齐 attempt、原始/调整 usage、本地 UsageLog、ActualCost、负余额、价格向量和
`earned_micros`，分别核对：

```text
local_actual_cost / local_base_cost ~= 1 + delta_applied
earned_micros ~= earning_from(final_usage, confirmed_listing_price_vector)
A6_public_quota / supplier_earning ~= 1.2
```

`earning_from`直接按同一请求的最终usage和已确认Listing价格向量计算。本地单位与A6 micros只比较比例；先扣除
`max(1 micros, solver记录的整数overshoot/量化包络)`，再应用相对偏差阈值。重复
回调/Replay不重复扣费，usage hash冲突拒绝二次结算。Listing共享价格事实异常增加
`OUTPUT_CONFIG_INVALID`；仅某`(account, transport_profile)`异常则只撤销该关系；Class2Adjustment偏差不扩散。earnings在
前10分钟每15秒、之后每5分钟查至24小时，并做daily late-arrival sweep；延迟只告警。15分钟内3个独立request持续偏差，
或单次相对偏差超过5%，按上述作用域修复。

## 12. 调度与错误分类

经济公式只做硬过滤，之后复用 Sub2API 现有调度。初选、手动指定、粘性、抢槽重检、failover、retry 和 Replay 的
每次 attempt 都经过同一硬门。

三个无成功账号出口互斥：

```text
请求前结构性 eligible=0，或当前 required transport eligible=0
-> 维护 NO_ELIGIBLE并异步下架

eligible>0且已派发的所有attempt均运行失败
-> 聚合错误后产生本地cooldown或平台incident

eligible>0但全部仅满并发/排队，未派发attempt
-> 普通service_busy，不加hold、不下架
```

错误按 `errorlist.md` 规范化：

- `USER_SHORT_CIRCUIT`：输入、入口鉴权、内容策略、客户端取消；不处罚账号、不扩池、不下架。
- `A6_ACCOUNT_RECOVERABLE`：余额、Provider Token、Pin或配置问题；摘账号并进入对应窄控制器。
- `UPSTREAM_AUTO_POOL_EXPAND`：沿用`errorlist.md`的canonical类型；Auto-Pool摘流扩池，Pin摘流并创建repin generation，
  不执行`target_n+1`或隐式切换route mode。
- `LISTING_COOLDOWN_TRIGGER`：平台、endpoint、egress、本地依赖、责任不明的 timeout/5xx/unknown，以及确实派发后
  候选全部失败；关闭该 Listing准入并异步下架。

单 attempt 只影响对应账号或 route；只有请求聚合终态影响 Listing。Class2/Class3 继续使用 Sub2API 原生账号状态机。

请求聚合顺序固定为：先对每个attempt执行账号/route级处置，再重算structural eligibility；归零时只维护
`NO_ELIGIBLE`。eligible仍大于0但终态集合含`LISTING_COOLDOWN_TRIGGER`或无法归责的错误时，才增加
`LOCAL_FAILURE_COOLDOWN`。全部只是busy时不创建状态事件。用户错误在scheduler前短路，不进入该聚合。

### 12.1 平台 Incident

明确的Cloudflare/A6全局、endpoint、egress或本地DB/Redis/Relay/Scheduler健康故障可直接建立对应scope的incident。
单次裸5xx、timeout、reset、missing terminal或unknown先只影响当前Listing；模糊错误满足以下任一条件才提升：

- 同一请求至少两个独立account/route identity经相同endpoint得到同一fingerprint；
- 60秒内第二个独立Listing得到同一fingerprint。

fingerprint至少包含：

```text
failure_domain | canonical_error_code | endpoint_host | endpoint_path_class |
http_method | egress_region | relay_generation | credential_scope
```

Listing/account/channel ID只作member。Incident Controller枚举scope内活动Listing并发membership事件，由Local Cooldown owner为本地故障成员
维护hold；REMOTE_COOLING成员不伪造hold，Incident只编排其reset顺序且不改observed。健康恢复后先让canary owner清hold或
执行cooling reset，再由普通Output Recovery resubmit；Marketplace可见且circuit无阻断形成witness。随后每批10个、间隔
5-15秒jitter恢复；任一失败即停。各owner只接受匹配generation的witness，channel scope只建一个父operation。

## 13. Listing 状态、下架与恢复

本地hold、远端期望、远端观测和执行计划必须正交：

```text
holds = { NO_ELIGIBLE, LOCAL_FAILURE_COOLDOWN, OUTPUT_CONFIG_INVALID, TEXT_CORPUS_INVALID,
  OUTPUT_KEY_ROTATION, BINDING_REVIEW, OPERATOR_HOLD }
desired = LISTED | UNLISTED
observed = LISTED | UNLISTED | COOLING | UNKNOWN
plan = NONE | ALIGN_UNLISTED | DIRECT_RELIST | COOLING_RESET | VERIFY_UNKNOWN
```

每个hold保存`owner_controller/source_key/source_generation`，只能由owner清除：

| hold | owner | 进入 | 自动退出 |
| --- | --- | --- | --- |
| `NO_ELIGIBLE` | Eligibility | structural或任一required transport eligible=0 | 同generation重算后每项恢复为正 |
| `LOCAL_FAILURE_COOLDOWN` | Local Cooldown | 第12节聚合终态 | 退避到期且eligible/本地readiness恢复；incident成员还需witness |
| `OUTPUT_CONFIG_INVALID` | Output Config | 共享价格、markup、Key或官方向量漂移 | 修复和current-generation读回匹配 |
| `TEXT_CORPUS_INVALID` | Text Corpus | 语料无效、耗尽或冲突 | 有效重选和相关读回完成 |
| `OUTPUT_KEY_ROTATION` | Key Rotation | 轮换开始 | 新Key切换或旧Key回滚完成且远端确认 |
| `BINDING_REVIEW` | Binding | owner冲突、多候选、消失或模型碰撞 | 管理员选择采用/绑定/退役并读回inventory |
| `OPERATOR_HOLD` | Operator | 人工设置 | 人工清除 |

binding/父Channel非ACTIVE或任一hold存在时`desired=UNLISTED`，否则为`LISTED`。`OPEN`还要求Marketplace按
listing/channel/model精确可见且available、circuit无cooling/half_open/model_excluded、generation匹配；其余均CLOSED。

显示和动作互斥：本地hold中的`LOCAL_FAILURE_COOLDOWN`显示`COOLING`并只等待本地恢复；无hold但observed=UNLISTED显示
`REMOTE_HOLD`并DIRECT_RELIST；observed=COOLING显示`REMOTE_COOLING`并COOLING_RESET；UNKNOWN只GET。普通GET失败仅
标记stale，保留最后事实；只有写结果不明、矛盾枚举或完整快照无法唯一解析才置UNKNOWN。

Planner固定优先级：继续current operation -> VERIFY_UNKNOWN -> 对齐desired=UNLISTED -> COOLING_RESET ->
DIRECT_RELIST -> 已LISTED且ready则NONE。Desired Projector只写desired/Outbox，Observer只写observed，Planner只选计划，
Output Recovery Reconciler是上下架唯一写者；`scope=channel`共用一个父operation，`scope=model`才逐Listing执行。

恢复合同：

- eligible变0增加`NO_ELIGIBLE`；恢复为正只清除此hold。终态失败增加`LOCAL_FAILURE_COOLDOWN`，退避15秒、120秒、
  10分钟后封顶；到期仅核对eligible、Key/绑定、Sub2依赖和原生账号健康，不发数据面Probe。
- 普通恢复为`resubmit/readback -> Marketplace精确可见且available -> circuit无阻断 -> generation匹配 -> OPEN`；超时保持
  CLOSED并重试。Supplier读回只审计写入，不是第二套可用性门。
- cooling epoch由circuit scope、Channel、model、state、cooling时间和error kind稳定哈希，同epoch只reset一次。等待
  `max(now+15s,cooling_until+jitter)`；5分钟内复发退避120秒、10分钟后封顶并告警，但保留自动/人工重放。
  `COOLING_RESET`固定执行`takedown/readback -> wait -> resubmit/readback -> Marketplace/circuit验证 -> OPEN`；中途变为
  UNLISTED仍继续原operation。所有轮询和超时可配置，不使用固定sleep。

## 14. 事件、频率与 Reconciler

- attempt同步检查模型、projection和经济generation；eligible跨0、绑定、Key、route、能力、账单及管理员参数变化在提交后发Outbox。
- Class1 marketplace/Top-N/A6 FX每60秒，运行错误立即fresh读取；platform models每5分钟，变更立即重算。
- circuit `limit=1000`，有活动Listing时每15秒及相关错误后读取；恰好1000项视为不完整并告警，不用“未出现”证明恢复。
- Supplier增量每60秒、完整inventory每5分钟；Class2复用现有刷新；Class3账单窗口15分钟；drift sweep每5分钟只读补漏。
- 普通调价按模型/generation合并30秒、每批10个且单Listing至少间隔5分钟；安全界、成本上涨和eligible归零立即处理。

所有目录、circuit和inventory轮询按`a6_credential_id`共享并扇出，不逐Listing重复请求。默认每凭据控制面并发2、全局并发10，
RPS均可配置；HTTP 429只延后对应读任务并沿用last-confirmed事实，不生成虚假down/cooling事件。

去重覆盖attempt、request terminal、circuit epoch、route/cooldown generation和pricing desired hash；同资源同generation仅一个
pending operation。只有aggregator可按`request_id+listing_binding_id`提交terminal event；重复stream/callback只补证据。
Class1路由修复按`account_id+route_generation+failure_fingerprint`唯一；eligible正数内变化只更新projection/指标。

Reconciler 合同：

1. 锁顺序为incident -> credential -> Provider Token/output Channel -> Listing；数据库lease默认30秒、每10秒续租，均可配置。
2. Outbox按资源/generation/kind唯一；旧generation先标superseded；远端写后GET，结果不明先读回。
3. 默认重试8轮、指数退避封顶30分钟，之后进入可见dead-letter并保持保守准入；人工重放复用identity并核对desired generation。
4. 请求路径只fast-block和写Outbox；Ban、Pin、Auto-Pool、Token及上下架只能由Reconciler写。
5. operation保存phase journal；接管者先读journal/远端事实，从最后确认phase继续。

## 15. 管理端

`/admin/users`增加`a6-channel`过滤；行内显示Channel、模型/Listing及eligible汇总、状态、负余额、读回和入口Key。抽屉按
Listing显示ck/public倍率、边界、delta范围、hold/desired/observed/plan，再展开账号class/sk/profit/delta/失败原因/generation。
输入部门管理账号、Provider Token/route/Ban/成本/能力/账单；输出部门管理Channel/Key/Listing/价格/delta/状态/收益；危险操作
必须标明部门和远端资源。

支持编辑或清空 delta override、账号 profit override、Class2 成本和 multiplier、Class3 declared sk及模型 override、
逐模型 hard min/max；公共文本界面仅有语料选择、随机重选和来源审计；提交前预览 eligibility 变化。

同时展示并允许按权限处理`ROUTE_UNKNOWN`、`AUTO_POOL_RANGE_EXHAUSTED`、orphan Token、dead-letter、平台incident、
`BINDING_REVIEW`及每种hold的owner/source generation。人工重放复用原operation identity；管理员只修改desired事实或选择
明确恢复动作，不直接篡改observed状态和历史readback。

## 16. 迁移

1. 按远端Channel聚合旧Managed Seller；结合binding、完整inventory、Key fingerprint/last4和成功入口日志选唯一anchor。
2. 一用户一Channel时保留`users.id`/Key原地转换；多用户一Channel时保留实际入口anchor并合并；一用户多Channel时保留一个
   anchor，其余Channel新建用户/唯一Key并逐个轮换。歧义进入`BINDING_REVIEW`。
3. 按第5节把旧组/alias canonicalize为`ToA6API`且倍率回填1.0。旧Listing delta只为切换时现有candidate建立
   `(listing,account,model)` nullable override并标记`legacy_managed_seller`；新账号只使用delta_auto。
4. 保存公共文本旧值并在ACTIVE前语料化；保存旧余额，删除巨额余额约束/触发器，以opening adjustment使新账本从0开始负扣。
5. Provider迁Class1独立Token；OAuth/API Key经管理员确认成本合同后进入Class2/Class3。
6. 先backfill/shadow-read，再按Channel flag切换；切换前下架，Key/模型/Ban/eligibility/Marketplace/circuit通过后恢复。
7. 不启动Provider shadow timer，不改变无关用户/账号；单Channel失败只回滚该Channel并保留历史事实。

每个Channel切换前保存本地用户/Key/分组、远端Channel Key、全部Listing状态和价格的回滚快照。未产生新流量时可按快照完整
恢复旧入口；已经产生新winner账本后只允许forward rollback：保留新账本、实例和负余额，恢复旧读取/调度路径时仍沿用当前
唯一远端Key，禁止复活非anchor Key或删除新事实。回滚完成同样要求逐项远端readback。

## 17. 验收标准

至少覆盖：

1. 唯一约束、稳定binding/instance、Decimal、加密脱敏、语料来源/冲突，以及一Channel一用户一Key/多Listing解析。
2. `concurrency=0`、`quota=0`、持续负余额、缓存一致性、`/v1/models`隔离和两类Key轮换。
3. Class1 Auto-Pool/Pin成本、Ban/BAN_DISCOVERY、扩缩池、repin、ROUTE_UNKNOWN和liquidity；Class2/3成本与账单恢复。
4. per-token各usage维度、delta usage结算生效、官方价/markup/FX、hard bounds及8桶稳定分布。
5. Listing+账号delta/override、transport profile、failover快照、stream首字节边界、winner唯一结算、ActualCost/负余额/A6 earnings；
   999/1000边界及与Class2Adjustment同时命中只合成一次。
6. 初选、粘性、抢槽、failover、retry、Replay同门；NO_ELIGIBLE、运行终态失败和纯busy三出口。
7. hold/desired/observed/plan正交、owner/退出、BINDING_REVIEW、完整下架/恢复/cooling reset。
8. Outbox乱序/重复、结果不明、租约、dead-letter、人工重放和platform incident提升/canary恢复。
9. Managed Seller聚合/拆分、组名canonicalize、opening adjustment、历史保留、forward rollback和现有生产能力回归。
交付时提供：迁移文件、后端与前端实现、测试结果、A6写后读证据、delta收益闭环证据、回滚步骤和运维参数清单。
