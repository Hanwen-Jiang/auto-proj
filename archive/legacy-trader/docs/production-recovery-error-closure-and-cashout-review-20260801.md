# 生产恢复、错误闭环与风险均衡提现审阅

- 审阅日期：2026-08-01（Asia/Shanghai）
- 证据截止：本文最后一次更新时的本地工作树、候选 release 证据，以及本轮已确认的生产读回
- 设计基线：`docs/a6-provider-topn-failover-design-20260728.md`，重点为 20.5、Phase 5-9
- 金额口径：本文使用“平台余额单位”；经营价格只使用 `K_CNY`，不引入 `K_USD`
- 当前结论：服务已通过手动换 Pin 恢复主要流量，但自动错误闭环、余额门禁修正版发布、Pin 定时任务恢复、
  Sub2API 账号级 fast-block 及 20.5 提现 apply 均未完成生产验收。因此当前状态是“业务恢复、自治未闭环”，
  不是“所有问题均已解决”。

## 1. 证据状态约定

本文严格区分以下状态：

| 状态 | 含义 |
| --- | --- |
| 已生产确认 | 已通过生产请求、账号读回、服务状态或当前 symlink 确认 |
| 本地已验证 | 工作树或不可变候选中已有实现和测试证据，但现网尚未消费 |
| 进行中 | 已定位并正在实现，测试或候选尚未冻结 |
| 待生产读回 | 缺少明确时间窗、计数、服务状态或数据库事实，不据此宣称完成 |
| 目标设计 | 20.5 已定义契约，但实现、远程 E2E 或 apply 门禁尚未达到退出条件 |

## 2. 核心结论

1. `Sam Altman`、`acme`、`MingZi` 有正用户余额却不调用 API，主要不是“余额不可使用”，而是两层我方
   控制逻辑叠加：旧 Pin 已不可用；同时 Trader 把低于固定继续经营门槛的已确认账号挡在真实 probe 之前。
2. 三个账号换 Pin 后真实探测成功，按本项目归因规则均属于我方路由恢复问题。正余额账号应继续真实 probe；
   只有请求实际返回 typed `insufficient_quota` 后，才按账号资金域摘流并换账号。
3. `fixed_merchant_unavailable` 的 HTTP 状态并不固定，已观察到 `402/429/502/503`。分类必须依据错误
   `code/message`，单独判断 HTTP 402 会漏报；命中后必须在提交前换账号/换 Pin，固定商家详情仅留内部审计。
4. 本轮最大错误源是已耗尽账号没有形成 durable account fast-block，导致后续请求反复选回同一账号；第二大
   错误源是固定商家不可用没有在所有状态码上触发 repin/failover。
5. 用户侧错误必须直接短路：入站鉴权 401、能力禁用、参数/请求约束错误和客户端 499 不得写入
   Provider/Pin 失败事实，也不得触发 repin、quarantine 或 publication 下架。
6. 风险均衡提现的资金前置能力已开始落地，但所有真实对外资金动作仍保持关闭。任何 migration 或 plan-only
   结果都不等同于真实提现已上线。
7. `2026-07-31 22:01 UTC` 的 Dashboard 最近 1 小时读回为：external sales `0`、seller income
   `151,732 micros`、Provider cost `219,072 micros`。仅按后两项机械相减为 `-67,340 micros`；该窗口
   收入低于 Provider 成本，且没有 external sales，不是“用户有余额却产生了足够覆盖成本的外部销售”。

## 3. 网络与部署不变量

- 内部 `sub2api/relay` 固定使用 `https://a6.a6api.com`。
- `a6.a6api.com` 从始至终直连 `45.61.235.131`，没有经过 Cloudflare，也没有发生切源。
- `api.a6api.com` 此前仍经过 Cloudflare，普通 Go Transport 可能触发 `403/1010`；现已切到同一源站。
- 生产控制面和主请求面仍在 `ssh us_cloud`；HK 继续保持 LB 权重 `0`，不得把 loopback/shadow 结果写成双活。
- `win_gw_lou` 只适合作为可失联的非权威计算 worker；其在线状态不得影响请求面 readiness、publication 或
  资金动作终态。
- 本轮恢复过程中没有批量重启 Sub2API、relay、Dashboard 或 Caddy。

## 4. 时间线

| 时间 | 事件 | 证据状态 |
| --- | --- | --- |
| 前序排障 | 确认内部 relay 应始终使用 `a6.a6api.com`；该域名一直直连 `45.61.235.131` | 已生产确认 |
| 2026-08-01 03:00（即 2026-07-31 19:00 UTC）起 | 建立近期错误聚合基线：331 次 quota、101 次 fixed merchant、18 次平台 routing 503、17 次 relay transport、3 次 no-channel/未分类 | 已生产确认；时间窗结束点待补 |
| 2026-08-01 03:30:51 | 当前 Trader release 路径时间戳：`20260731T193051Z-v97-token-name-corpus` | 已生产确认 |
| 2026-08-01 04:10:59（即 2026-07-31 20:10:59 UTC） | Pin 轮次因 `provider_probe_time_budget_exhausted` 失败并停止；正余额 `118,564 / 121,502 / 120,414` 被旧门槛标成 `blocked_low_balance` | 已生产确认 |
| 本轮手动恢复，精确时间待补 | `Sam Altman`、`acme`、`MingZi` 换 Pin 后真实探测成功；“恋人”建立池模式账号并换 Pin 成功 | 已生产确认 |
| 手动恢复后样本窗，精确起止待补 | 约 `63 x 200`、`8 x 502`、`10 x 499`、`2 x 401` | 已生产确认；固定窗口待补 |
| 2026-08-01 06:01（即 2026-07-31 22:01 UTC） | Dashboard 最近 1 小时：external sales `0`、seller income `151,732 micros`、Provider cost `219,072 micros` | 已生产确认；完整利润账务项待审阅 |
| 最近两轮 Collector，精确轮次时间待补 | 均为 `sessions_found=62, eligible=51, collected=51, failed=0, skipped=11, errors={}` | 已生产确认；历史 13 项根因待日志确认 |
| 2026-08-01 03:59:59 | 生成 v98 Seller gate/focus resume 候选；只读验证完成，未部署 | 本地已验证 |
| 2026-08-01，本次审阅前 | Trader 余额门禁本地改为“已确认 Pin 且用户余额 `> 0` 可继续真实 probe” | 本地实现；完整测试/发布读回待补 |
| 2026-08-01，本次审阅前 | migration `0049`、Provider liquidity reservation/UNKNOWN reconcile 本地落地，真实 apply 默认关闭 | 本地实现；生产 migration/service 状态待补 |
| 2026-08-01，进行中 | Sub2API typed error、跨状态 fixed merchant、账号级 quota fast-block、同请求排除和用户错误短路 | 进行中；未发布 |
| 2026-08-01，本次审阅前 | migration `0050`、20.5 plan-only Cashout Orchestrator、风险/公平排序及资金 intent 状态机本地落地 | 本地核心实现；真实提现关闭、生产未部署 |

## 5. 有余额却不调用 API：根因与修复

### 5.1 已确认事实

- `118,564 / 121,502 / 120,414` 这类低于 `500,000` 的正用户余额仍可用于请求，仅凭低于一个固定
  整单位就判定账号不可调用。
- 旧逻辑对已确认 Pin 的 Provider 使用了固定继续经营余额门槛，账号在进入真实 probe 前就被标成
  `blocked_low_balance`；因此系统既没有尝试实际请求，也拿不到权威 `insufficient_quota` 错误。
- 旧 Pin 同时发生 `fixed_merchant_unavailable`，换 Pin 后三家均恢复，证明账号、余额和 API 能力并非整体失效。

### 5.2 手动恢复读回

| 商户 | merchant / account | 旧 Pin | 新 Pin | 结果 |
| --- | --- | --- | --- | --- |
| `Sam Altman` | `5 / 45895` | `1550`，返回 fixed merchant | `649` | 真实 probe 成功，`schedulable=true` |
| `acme` | `7 / 45897` | `1188`，返回 fixed merchant | `649` | probe 成功，随后承接数十个真实 `200` |
| `MingZi` | `19 / 45896` | `1301`，返回 fixed merchant | `1242` | 真实 probe 成功，`schedulable=true` |
| “恋人” | `6742 / 45917` | 新建池模式账号后 Pin `1170` 返回 fixed merchant | `649` | probe 成功并启用 |
| `VegaHub` | `account 45915` | `1170` | 待自动 repin | 仍间歇 fixed merchant |
| `SwiftRelay` | `account 45916` | - | - | 当前未调度，原因待生产读回 |

### 5.3 永久规则

```text
首次入池：仍需满足 initial funding floor 或存在可确认的整单位补款能力
已确认 Pin 且 user_balance > 0：允许真实 probe
真实 probe 返回 insufficient_quota：ScopeAccount durable fast-block
同一请求：排除失败 account，不得 A -> A
后续控制面：补款、替补或确认余额恢复后再解除 block
```

本地 `_provider_balance_ready()` 已按上述继续经营语义修改；它与临时 residual Provider 的 reserve 规则分开，
不会让退休余额侵占 reserve。发布后必须以三家低余额账号进行真实读回，证明不再产生旧式
`blocked_low_balance`。

## 6. 错误清单与闭环

### 6.1 聚合窗口

当前计数从 `2026-07-31 19:00 UTC` 起。窗口结束点、查询语句/日志游标和去重口径尚未固化到审计附件，
明日审阅必须补齐；在补齐前这些数字用于故障排序，不作为长期 SLO 分母。

| 错误 | 次数 | 代表样例 | 责任域 | 即时动作 | 永久闭环 | 验证与当前状态 |
| --- | ---: | --- | --- | --- | --- | --- |
| `insufficient_quota` | 331 | 旧 account `45914` 余额耗尽 | 我方账号池；上游余额事实是触发信号 | 排除该 account，立即选下一个 account | typed `ScopeAccount`、durable fast-block、同请求 account exclusion、补款/替补后权威解除 | Go 实现进行中；生产尚未发布，待验证 A 不再重选 A |
| `fixed_merchant_unavailable` | 101 | HTTP `402/429/502/503` 均可能携带该 code | 我方 Pin/路由；换 Pin 可恢复 | 提交前换 account/Pin，不透传固定商家详情 | 按 code/message 跨状态分类，写 repin 事实，quarantine 旧 Pin，probe 新 Pin 后恢复 | 手动换 Pin 已证明可恢复；Go 跨状态修复进行中、未发布 |
| 平台 routing 503 | 18 | `Service temporarily unavailable`，当时无可调度账号 | 我方池坍缩/publication | 恢复至少一个 READY 账号并重算路由 | 最小池容量、VACANT/publication 一致性、失败账号即时替补 | 手动恢复后成功率上升；canonical READY、generation、publication 待生产读回 |
| relay transport failure | 17 | 约 30 秒超时 | 首次记 `unknown`；若换 account 成功则归我方 | 首包/提交前换 account；客户端已断开则停止 | pre-send deadline、同请求排除、SSE/HTTP/WS 一致；记录 winning attempt | 永久实现/生产读回待补；单纯延长总超时不是闭环 |
| `no_available_channel` / 未分类上游错误 | 3 | 无可用渠道或尚未分类的上游错误 | 默认我方路由诊断域，直到权威证据改判 | 短路当前候选，进入 next account/repin | typed error + probe/quarantine；禁止透传内部建议 | 本地 Python classifier 仅覆盖部分状态；Go 完整链路进行中 |
| 入站鉴权 401 | 完整窗口计数待补；恢复后样本 2 | 用户 token/session 无效 | 用户请求域 | 直接返回稳定用户错误 | `Provider/Pin failure writes = 0`，无 repin、无模型下架 | 短路契约需在 HTTP/SSE/WS 回归并做生产计数读回 |
| image generation disabled / 能力不匹配 | 待生产读回 | 用户请求了未启用能力 | 用户请求域 | 直接返回稳定能力错误 | 不污染账号、Pin、渠道健康和 publication | 待补生产分类计数 |
| 客户端 499 | 完整窗口计数待补；恢复后样本 10 | 客户端在结果前断开 | client transport | 取消剩余 attempt，停止新上游调用 | `Provider/Pin failure writes = 0`；已提交 attempt 只做账务收口 | 现有短路行为已存在；需生产验证无 repin 事件 |
| 历史 13 个商户采集异常 | 历史 13；当前 0 | Dashboard 历史告警；最近两轮均 `failed=0, errors={}` | 历史责任待分类 | 当前无需摘流；保持 Collector 正常轮询 | 按 session、身份、HTTP、解析、超时逐商户归因；session 过期先统一 refresh | 当前已恢复为 0；历史商户清单和逐项根因待保留日志确认 |
| 历史 Cloudflare `403/1010` | 本窗口计数待补 | 普通 Go Transport 经 `api.a6api.com` | 我方 endpoint 选择 | 内部 relay 使用 `a6.a6api.com` | 配置测试锁定 host；重建连接池后 HTTP/SSE/WS 复测 | endpoint 不变量已确认；当前连接池和请求 host 待读回 |

### 6.2 归因规则

按以下顺序判定，HTTP 状态码只作为证据之一：

1. **换 Pin 或换同池账号能恢复，就是我方问题。** 包括 fixed merchant、旧 Pin 冷却、能力不匹配但池中
   存在可用候选、无可用渠道以及因账号未替补造成的池耗尽。
2. `insufficient_quota` 是账号级权威余额事实，但“继续把请求发给已耗尽账号”属于我方。它应阻断单账号，
   不是阻断整个模型，也不是对最终用户透传上游充值建议。
3. 用户域只根据入站请求事实认定：入站鉴权失败、用户参数错误、用户未启用的能力、用户主动取消/499。
   这些错误直接短路，且 `Provider failure writes = 0`。
4. 传输失败在首个语义事件提交前可换候选；换候选成功后归我方可恢复路由。所有候选均失败时保留
   `unknown/provider transport` 诊断，不凭一次超时永久下架渠道。
5. 上游错误 body、商家名称、request ID 和“切换固定商家”的建议都属于内部诊断信息；用户响应只返回稳定
   公共错误码。

### 6.3 明确禁止的错误扩散

```text
用户 401 / capability / invalid request / 499
  -> 结束请求
  -> attempt_failure(provider) writes = 0
  -> Pin failure journal writes = 0
  -> 不触发 repin/quarantine/publication

fixed merchant / quota / no channel
  -> 写 typed internal fact
  -> 排除失败候选
  -> 提交前尝试 next account
  -> 只有候选耗尽才返回稳定公共错误
```

### 6.4 最近 1 小时经营读回

`2026-07-31 22:01 UTC` 的 Dashboard 快照：

```text
external sales              0
seller_income_micros   151,732
provider_cost_micros   219,072
两项机械差额            -67,340 micros
```

这组事实说明该窗口的直接问题是“外部销售为 0，同时 Provider 消耗仍在发生”，而不是 Collector 当前仍批量
失败。`seller_income_micros`、external sales 和最终可确认利润属于不同账务字段，明日需把请求 ID、销售、
Provider adjusted usage、客户扣费和 settlement 用同一时间边界对齐后再给出完整利润结论。

## 7. 已处理、正在处理和未关闭项

### 7.1 已生产处理

- 四个商户通过建池/换 Pin 恢复真实 probe，其中 `acme` 已承接数十个真实 `200`。
- `a6-provider` 账号使用池模式；“恋人”已按池模式创建 account `45917`。
- 内部 relay endpoint 已明确为 `a6.a6api.com`，且该域一直直连 `45.61.235.131`。
- 当前生产 Trader 仍为不可变 v97，数据面未因本轮修复被批量重启。

### 7.2 本地已处理、待发布

- 已确认 Pin 的 Provider 继续经营余额门禁改为 `user_balance > 0`，让真实 probe 产生权威 quota 事实。
- Provider liquidity migration `0049` 增加共享资金 reservation、Provider reserve 和 liquidity event 投影；
  UNKNOWN transfer 只对账，不重复提交。
- v98 Seller gate/focus resume 候选完成只读构建与验证，但它不包含上述最新余额门禁补丁，也不包含正在进行的
  Sub2API typed error/20.5 cashout 实现，尚不满足本轮最终综合 release 的范围。

### 7.3 尚未关闭

- `a6-trader-pin.timer` 与 `a6-trader-pin.path` 为避免旧门禁再次摘流而临时停止；修正版发布并手动跑通一轮后
  必须恢复并 `reset-failed`。当前实际 active/enabled 状态待生产读回。
- fixed merchant 的 `429/502/503` 跨状态识别、第二 quota code、账号级 durable fast-block、同请求排除和
  selection exhaustion 仍在实现/测试；现网尚未消费。
- 手动恢复后仍有 8 次 502，需按 error code、account、Pin、是否首包提交和最终 attempt 逐条拆分。
- `VegaHub` 间歇 fixed merchant、`SwiftRelay` 未调度仍缺生产终态读回。Collector 当前已恢复为 0 失败，
  历史 13 项的逐项根因仍需从保留日志确认。
- Dashboard 的 canonical READY/VACANT、routing generation、cost generation、publication desired/actual
  必须恢复为有值且相互一致；此前 `-- / -- / unknown` 表示控制面尚无有效读模型。
- HK 正式小流量、区域协调和负载均衡尚未开始，LB 权重继续为 `0`。

## 8. 20.5 风险均衡提现编排

### 8.1 不变量

```text
cashout_eligible = max(0, withdrawable_current
                          - provider_reserve
                          - local_reserved_other)

资金优先级：
已提交且待确认 intent
-> READY 账号立即补款
-> Provider 24 小时运行保留金
-> 普通 crypto 提现
-> 高风险 callback sweep
```

- 普通提现最低 gross 为 50 平台余额单位，默认固定手续费 3；目标是触发线，触发后提交完整
  `cashout_eligible`，不是固定只提 50。
- callback 计划费率 fallback 为 `1/6`，只在其期望净值高于等待普通提现时进入候选；`11.35%` 只是在目标
  恰为 50、两通道成功率为 1 且无额外成本时的 bootstrap 临界点，不是全局常量。
- 调度顺序按 merchant gross source-debit coverage 升序、风险敞口降序、最久未提现优先，再以 merchant ID
  确定性打破平局；最近 100 次渠道可用率不参与提现风险模型。
- 高风险在役 Provider 必须先 `DRAINING -> replacement READY 或 VACANT`，再释放 reserve 并执行 cashout。
- 每个 merchant 最多一个 nonterminal 资金 intent；远程响应不确定时进入 `UNKNOWN/RECONCILING`，只对账，
  不换 route/receiver 重发。

### 8.2 当前落地状态

| 组成 | 当前状态 | 证据/差距 |
| --- | --- | --- |
| financial snapshot、withdrawal event、legacy 提现账户 | 已有基础事实 | 字段语义和新鲜度仍需 fixture/生产读回 |
| `merchant_fund_reservations` | 本地已实现 | migration `0049`；生产是否应用待读回 |
| `provider_fund_reserves` | 本地已实现 | immediate refill/pending top-up reserve；24h p95 forecast 尚需历史样本校准 |
| Provider liquidity reconcile | 本地已实现、默认关闭 | plan/apply 两层门禁、整数单位补款、UNKNOWN readback；真实小额 E2E 待做 |
| wallet principal/route/assignment | 本地 schema 已实现 | migration `0050`；inventory 为 21 principal / 61 route，legacy backfill/双读仍需真库和远程账号读回 |
| risk/EV planner、fair scheduler | 本地已实现 | 确定性 target/jitter、动态 `p_star`、route EV、coverage/risk/oldest 排序和 anti-starvation 已有单元测试 |
| plan-only Cashout Orchestrator | 本地已实现、默认关闭 | `enabled=false`，启用 plan-only 时 `remote_write_count=0`；尚无生产 CLI/service 连续证据 |
| crypto preview/submit/status | 目标设计/进行中 | 尚无生产真实提现确认样本 |
| callback 三腿 ledger | 本地 schema/store 契约已实现 | sender debit、fee、receiver credit 的远程 adapter/canary 样本为 0 |
| UNKNOWN cashout reconcile | 本地 store 状态机已实现 | 目前为单元/源契约证据；仍需真库故障注入证明 response lost/crash 下远程提交次数为 1 |
| Dashboard cashout 审计 | 待实现/接线 | coverage、risk exposure、reserve、unknown age 尚无生产读回 |

本地验证证据（本次只读复跑）：

```text
tests/test_trade_cashout_orchestrator.py
  14 passed

cashout + liquidity + topup + inventory + trade_db 定向组合
  115 passed

tests/test_trade_pin_runner.py
  59 passed

migration pairs
  0049 source/package identical
  0050 source/package identical
```

以上没有 PostgreSQL 真库 cashout integration、A6 远程 preview/submit/status 或 callback canary 证据，
所以只支撑“本地核心实现”，不支撑“生产资金闭环完成”。

### 8.3 apply 门禁

- Provider liquidity service 同时要求：
  `PROVIDER_LIQUIDITY_RECONCILE_ENABLED=true`、CLI `--apply`、精确确认串
  `a6-provider-liquidity-v1`。默认环境 gate 为关闭。
- Cashout 必须保持：
  `treasury_standard_withdraw_apply_enabled=false`、
  `treasury_callback_sweep_apply_enabled=false`。
- plan-only 只能持久化决策、reservation 和审计事实，不得调用远程 submit。
- 关闭 apply 只阻止新远程资金动作；已处于 `SUBMITTED/PENDING/UNKNOWN` 的 intent 必须继续由只读 reconciler
  收口；release 回滚同时保留这些对账任务。

### 8.4 开启真实资金动作前的退出门槛

1. 连续 7 天 plan 与 A6 权威余额语义一致。
2. 普通提现和 callback 各至少 20 个确认样本。
3. 重复资金动作、callback 三腿账务不平、Provider reserve 透支均为 0。
4. 所有 UNKNOWN 最终进入可解释终态，故障注入下远程 submit 次数仍为 1。
5. 高风险合格余额 24 小时内进入执行队列，且在役账号先完成替补/VACANT。
6. 真实 preview 的手续费、net、route、remote withdrawal ID 和最终余额效果全部读回。

未达到以上条件前，20.5 的正确状态是“plan-only 落地中、apply 关闭”。

## 9. Release、发布与回滚

### 9.1 当前生产与候选

| 项目 | 值 | 状态 |
| --- | --- | --- |
| 当前生产 release | `/www/a6-trader/releases/20260731T193051Z-v97-token-name-corpus` | 已生产确认 |
| v98 候选 | `.codex-tmp/release-candidates/20260731T195959Z-v98-seller-gates-focus-resume-rc1` | 未部署 |
| v98 wheel SHA256 | `bcd6d07ebb7d566256f17a7fe7f7a9edca9dde43714f4778382f32b5b7d6ccad` | 本地验证 |
| v98 验证 | Seller suite 226 passed；focus combined 104 passed；Ruff/RECORD/parent diff 通过 | 候选证据 |
| 本轮最终综合 release | 尚未冻结 | 必须包含余额门禁；Sub2API 独立候选需单独版本化 |

### 9.2 发布检查

1. 从当前生产 v97 构造新的不可变目录，不直接修改 `/opt/a6-trader/current` 指向内容。
2. 精确列出相对 v97 的 Python、migration、script 和 unit diff；拒绝未在 allowlist 的文件漂移。
3. 在隔离 venv 安装 wheel，核对 RECORD、package manifest、关键源文件 hash 和 Python 3.10 兼容性。
4. 先应用仅追加、可幂等的 migration；核对表/约束/权限，不执行破坏性 schema 回退。
5. 原子切换 Trader symlink；只重启受影响的 Trader oneshot/timer，不批量重启数据面。
6. 手动执行一轮 Pin，读回三家低余额账号、fixed merchant repin、quota account block 和池最小容量。
7. 恢复 Pin timer/path，观察至少多个连续轮次；再检查 READY/VACANT、routing/cost generation 和 publication。
8. Sub2API typed error 使用独立不可变候选发布，按 HTTP、SSE、WebSocket 三协议逐项灰度验证。

### 9.3 回滚原则

- Trader 代码回滚：将 symlink 原子指回 v97，保留新 migration 和事实表；关闭新 worker 的 feature gate。
- Sub2API 回滚：回到上一不可变二进制/服务定义；不清空 durable failure、reservation 或 UNKNOWN intent。
- 资金回滚：关闭两个 treasury apply 开关；已提交 intent 继续只读 reconcile，禁止再次 submit。
- 区域回滚：HK LB 权重保持/恢复为 `0`；US 继续作为权威控制面。
- 回滚后必须重新读回 symlink、unit ExecStart、环境 gate、timer 状态和最近一轮结果，命令退出码只作辅助证据。

## 10. 明日审阅检查表

### 10.1 业务恢复

- [ ] 固化错误基线的准确结束时间、查询/日志游标和去重口径。
- [ ] 读取最近 1 小时和最近 24 小时：总请求、成功、各 typed error、收入、成本、净值，价格只用 `K_CNY`。
- [ ] 核对 `Sam Altman`、`acme`、`MingZi`、“恋人”仍 `schedulable=true` 且最近真实请求成功。
- [ ] 解释并关闭 `VegaHub` fixed merchant、`SwiftRelay` 未调度和手动恢复后 8 次 502。
- [ ] 从保留日志导出历史 13 个采集异常商户清单：merchant、session、HTTP/error code、last success、
  refresh 结果；同时确认最近两轮 `failed=0` 没有回退。

### 10.2 自动错误闭环

- [ ] fixed merchant 在 `402/429/502/503` 上均归一化为同一 typed provider failure。
- [ ] `insufficient_quota` 和 `account_quota_insufficient` 均形成账号级 durable fast-block。
- [ ] 同一请求失败后 account exclusion 生效，证明不存在 A -> A。
- [ ] HTTP、SSE 首个语义事件提交前、WebSocket handshake 三条路径均切换到下一候选。
- [ ] 用户 401、能力禁用、参数错误、499 的 Provider/Pin failure write 数为 0。
- [ ] 用户响应不含固定商家名称、上游 request ID、充值或切换商家的内部建议。

### 10.3 Trader 与 publication

- [ ] 当前 release symlink、manifest SHA256 和实际 unit ExecStart 一致。
- [ ] 三家正低余额账号不再因固定 continuation floor 产生 `blocked_low_balance`。
- [ ] Pin timer/path 已恢复且连续轮次成功；不存在持续 failed unit。
- [ ] Dashboard canonical routing generation、cost generation、READY/VACANT、publication desired/actual 有值且一致。
- [ ] 模型下架只由 canonical publication 判定，用户错误和采集短暂异常不触发全模型下架。

### 10.4 20.5 与资金

- [ ] migration `0049` 在目标库的 checksum、表、约束、索引和 `a6trader` 权限读回一致。
- [ ] liquidity service 默认 disabled；开启时 env gate、`--apply` 和精确确认串三者同时生效。
- [ ] plan-only 一轮的 `cashout_eligible`、reserve、risk、target、method、coverage 和排序可重复重放。
- [ ] 同 merchant 只能存在一个 active reservation/nonterminal intent。
- [ ] 所有 treasury apply 开关保持 false，远程 crypto/callback submit 计数为 0。
- [ ] UNKNOWN reconcile 故障注入通过后，才安排隔离 merchant 的单 intent 小额 E2E。

## 11. 自治完成定义

只有同时满足以下条件，才可以把系统描述为“可自行运转”：

- 低价优先、最近成功优先、延迟次优的 Pin 策略持续工作，失败 Pin 可自动替换并恢复；
- 账号额度耗尽只阻断该账号，固定商家错误可在提交前换候选，用户错误直接短路；
- READY/VACANT、routing/cost generation、publication 形成单一 canonical 闭环；
- session refresh、采集、Seller gate 和上架链路可以自动恢复，短暂失败不会把全池摘空；
- 资金先保障 Provider 续航，cashout plan 可重放、apply 有独立门禁、UNKNOWN 可唯一对账；
- US 持续作为权威控制面，HK 未达区域退出条件前保持 LB 权重 `0`；
- 最近 1 小时和 24 小时的盈利、错误、可用率都能从审计事实解释，而不是依赖人工猜测。

当前离该定义仍有明确工作：发布并验证余额门禁、完成并发布 Sub2API typed failover、恢复 Pin 定时控制、
关闭采集与 publication 空状态、完成 20.5 plan-only 连续证据及真实资金 E2E。上述未关闭项全部有生产读回后，
再更新本文结论。
