# a6api-auto

`a6api-auto` is a pure-HTTP workflow for one a6api account:

1. Randomly reserve one address from an owner-provided QQ pool.
2. Snapshot the QQ mailbox, request an a6api verification code, and correlate only mail from `A6API <noreply@notify.a6api.com>` with subject `A6API 邮箱验证码`.
3. Register and log in through the current New-API endpoints.
4. Create exactly one idempotently named a6api key and reveal it.
5. Read every model from the supplied upstream `/models` or `/v1/models`; fall back to a6api's `/api/supplier/fetch-models` adapter.
6. Submit one supplier and channel request using one of 200 bundled templates, then reconcile it through `/api/supplier/me/requests`.
7. Save account credentials, cookies, the generated key, and request IDs in an owner-only session file.

No browser or Playwright runtime is used.

## Install

```bash
cd ~/Documents/code/a6api-auto
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Phase 0 交易事实层

Phase 0 已包含版本化数据库迁移、持久化会话的秘密脱敏元数据导入、A6 只读 Collector、
Enodot Admin 客户端，以及只读总览面板。Collector 会写入 PostgreSQL 事实表，但不会向
A6 发起余额划转、Pin、定价、上下架或提现请求；Enodot Admin 的写方法也不会被以下四个
命令自动调用。

安装 PostgreSQL 运行依赖：

```bash
.venv/bin/pip install -e '.[dev,trade]'
```

服务端使用的环境变量名称：

| 名称 | 用途 |
| --- | --- |
| `A6_TRADE_DATABASE_DSN` | Phase 0 PostgreSQL 连接；四个命令的默认 DSN 来源 |
| `AI_ENODOT_SHOP_ADMIN_KEY` | Enodot Admin 客户端认证；不进入浏览器或 Collector 输出 |

DSN 也可放入权限为 `0600` 的文件并通过 `--database-dsn-file` 读取；该参数优先于
`--database-dsn-env`。CLI 没有明文 DSN 参数。

### 本地运行

按固定顺序运行：

```bash
.venv/bin/a6api-auto trade-migrate

.venv/bin/a6api-auto trade-import-sessions \
  --sessions-dir ~/.config/a6api-auto/sessions

.venv/bin/a6api-auto trade-refresh-sessions-once \
  --sessions-dir ~/.config/a6api-auto/sessions \
  --proxy-map-file ~/.config/a6api-auto/trade-proxy-map.secret.json \
  --max-workers 4

.venv/bin/a6api-auto trade-collect-once \
  --sessions-dir ~/.config/a6api-auto/sessions \
  --proxy-map-file ~/.config/a6api-auto/trade-proxy-map.secret.json

.venv/bin/a6api-auto trade-dashboard \
  --sessions-dir ~/.config/a6api-auto/sessions \
  --host 127.0.0.1 \
  --port 8765
```

每个商户都是 Seller。`Provider` 和 `Observer` 是互斥的运行职责；分配新职责会
原子替换旧职责，单独使用 `--seller` 会清除当前职责但保留 Seller 身份。首批职责分配默认
只预览，整批校验通过后才可显式提交：

```bash
.venv/bin/a6api-auto trade-assign-roles \
  --provider 5 --provider 19 \
  --seller 4 --seller 7 --seller 6 \
  --observer 1 --observer 2 --observer 3

.venv/bin/a6api-auto trade-assign-roles \
  --provider 5 --provider 19 \
  --seller 4 --seller 7 --seller 6 \
  --observer 1 --observer 2 --observer 3 \
  --config-version phase1-seller-duty-20260725-v2 \
  --apply
```

重复执行角色命令不会重复写审计。后续 `trade-import-sessions` 重导入只更新会话元数据，保留
数据库中已经分配的运行职责。

`trade-refresh-sessions-once` 跳过 `allocated` 会话以及缺少用户 ID 的记录，对其余非终态会话
按固定代理先调用 `self_user` 验证 Cookie；失效时才使用已有凭据重新登录，并严格核对登录后的
用户 ID。成功后原子写回 Cookie，读取 supplier profile 同步 `supplier_id/status`，再重导入该
商户的脱敏元数据。`abandoned` 不发网络请求，只作为 tombstone 写回数据库；商户主键、角色和
既有身份字段保留，历史事实与审计记录不删除。单账号失败只累计到 `errors`，其余账号继续；
摘要仅包含计数和稳定错误码，不包含路径、账号、Cookie、密码、代理或 DSN。部分失败时 CLI
退出码为 `2`，供人工告警使用。网络刷新使用有界并发，`--max-workers` 默认为 `4`、允许 `1-16`；
每个 worker 与按需内联重登共享 `.<session>.auth.lock`，锁住完整的 session 读改写。所有网络任务
结束后才由主线程按 session path 顺序写 merchant metadata，避免跨线程共享 psycopg 连接。摘要中的
`cookie_valid/relogged` 分别记录完成认证检查时沿用有效 Cookie 和实际重新登录的会话数。生产
systemd 对约 46 个会话显式使用 `--max-workers 8`，同时保留 4 分钟 oneshot 上限。

所有已提交且具有完整 A6 用户/商户 ID 的 Seller 都应创建或保留 Enodot Key、A6 Channel 和
Listing。`--all-sellers` 按数据库商户 ID 稳定排序选择全部 Seller；未完成供应商申请的账号
报告为 `pending/not_ready`，不阻断已就绪 Seller。Provider 和 Observer 只是不参与筛选的运行
职责。命令默认只读预览，确认后再增加 `--apply`：

```bash
.venv/bin/a6api-auto trade-ensure-seller-resources \
  --all-sellers \
  --group-id 26 \
  --delta 0.5 \
  --cost-multiplier 0.12 \
  --listing-description "Team 订阅来源，适合日常对话与代码任务" \
  --proxy-map-file /var/lib/a6-trader/secrets/proxy-map.json
```

试水或排障时可用一个或多个 `--merchant MERCHANT_ID` 替代 `--all-sellers`；两种选择方式互斥。
职责在 Observer 与 Provider 之间切换不会改变受管资源 UID，也不会重建已有 Channel 或 Listing。
Price Band 使用权限为 `0600` 的 `--listing-description-map-file`，为每个选中 Merchant 提供
独立的模型来源说明；示例见 `deploy/listing-descriptions.example.json`。Listing 所有权依据已绑定的
`listing_id`、`channel_id`、`own_model` 和 `platform_model` 校验，运营人员调整说明不会丢失绑定。

首次导入应报告 19 行，其中 16 个 `supplier_submitted`、3 个 `allocated`。导入器默认角色为
`seller`，并且只写会话引用、指纹、A6 用户/商户 ID、状态和代理元数据；邮箱、密码、
Cookie 和完整 Key 留在原 `0600` 会话文件中。重复执行迁移和导入是幂等的。

`trade-collect-once` 必须在 `--proxy-map-file` 与 `--allow-direct` 中二选一。19 会话基线应使用
权限为 `0600` 的代理映射 JSON：键为 A6 用户 ID，值为固定代理 URL。任何 A6 请求开始前，
CLI 会校验所有可采集会话都已映射，且不同账号没有复用代理 URL；部署时这些 URL 还应
对应不同且稳定的出口 IP。`--allow-direct` 只允许目录中最多一个可采集会话，不能用于 19 会话
目录。Collector 跳过尚未提交的会话，成功完成时输出 `upstream_mutations: false`，并使用
数据库水位增量同步收益和消费日志。

面板地址为 [http://127.0.0.1:8765](http://127.0.0.1:8765)，健康检查为
`/healthz`。总览和商户 API 支持 `5m`、`1h`、`5h`、`12h`、`24h`、`7d`、`30d`、
`mtd` 和 `all` 窗口。面板只读取聚合事实，不接触会话、A6 Key 或 Enodot Admin Key。

面板中的 `honest_profit_micros` 使用以下经营口径：

```text
Enodot 外部客户确认收入
  - 唯一匹配到 Enodot 请求的真实 A6 Provider 成本
  + Seller 外部收益（counterparty_class = 'external'）
  - (Probe 毛成本 - 对应 Seller 回流)
  - (抖加毛成本 - 抖加回流)
  - 已完成提现手续费
```

每条 A6 收益事件按 `request_id` 与 `a6_provider_usage_events` 双向匹配后打上
`counterparty_class`：命中 `a6_channel_booster_tokens` 的是 `own_boost`（抖加自买），
命中普通 Provider Token 的是 `own_pin`（Pin 自有渠道排除失效，属事故），
超过结算窗口仍无匹配的是 `external`。**只有 `external` 计入收入**；`own_boost` 的回流
不是收入，它与抖加毛支出一起以 `boost_net_loss_micros` 计为损耗 —— 平台抽成 16.67%
使自买自卖恒为负收益。

`attribution_coverage` 同时计入三类缺口：未归属销售请求、未匹配 Provider 成本事件、
未分类收益事件。**覆盖率低于 0.9 时 `honest_profit_micros` 返回 `null`**，因为把未匹配
成本按零处理的利润数字必然虚高。

收入和成本不要求 Enodot 与 A6 的 `request_id` 相同。Repository 使用已绑定的 Enodot
`account_id`、A6 Token、模型、五秒时间窗和可用的输入/输出 Token 标识做双向唯一匹配。
Binding 后仍无法唯一匹配的 A6 Provider 成本单列为待归属成本，利润状态为 `partial` 且金额
为 `null`，不会把缺失收入按零处理。Managed Seller Key 的 `actual_cost` 以
`managed_internal_debit_micros` 单独展示为内部扣账，不重复作为外部成本扣减。接口同时返回
`confirmed`、`partial`、`pending`、`unavailable` 和 `not_applicable` 状态；没有确认销售额时
利润率为 `null`。首次导入曾受收益分页上限影响的部署，可用
`scripts/backfill_trade_earnings.py` 逐页幂等回填；脚本每页提交并在私密状态文件中保存续传水位。

Owner 激活和其他内部操作通过 `a6_trade.enodot_usage_classification_rules` 按 Enodot User/Key
及生效时间分类。命中规则的 usage 不进入外部收入，但唯一匹配的真实 A6 Provider 成本保留在
`owner_internal_provider_cost_micros`。Pin 健康探活则通过 `provider_pin_probe` 审计动作与 A6
事件做双向唯一匹配，从普通 Provider 成本移入 `pin_probe_cost_micros` 和 Probe 净损耗。

所有 A6 原始金额和 Quota 通过对应 Session 的 `quota_per_unit` 换算成美元；当前默认值为
`500000`，即 `500000 A6 quota = $1`。数据库事实表保留 A6 原始整数，Repository 对每个商户
分别换算成 USD 微单位后再汇总，Dashboard 因此统一显示 `$`。商户余额转用户余额的最低金额
为 `$1`，也就是该商户当前的一个 `quota_per_unit`；每次仍先调用 Preview，并以返回结果为准。

加密货币提现账户使用一个权限为 `0600` 的私有池文件，格式为：

```json
[
  {"network": "ERC20", "address": "ACCOUNT_ADDRESS_1"},
  {"network": "TRC20", "address": "ACCOUNT_ADDRESS_2"}
]
```

池内完整地址只保存在该文件和发往 A6 的创建请求中；PostgreSQL 只保存网络、地址 SHA-256、
掩码和私密文件引用。先运行 dry-run 查看已有账户、需要创建的数量和池缺口：

尚未提供新地址时可以先以 `[]` 初始化池；该状态只支持盘点和登记已有账户，缺少账户的商户
会计入 `shortfall`，不会生成或提交占位地址。

```bash
.venv/bin/a6api-auto trade-bind-withdraw-accounts \
  --sessions-dir /var/lib/a6-trader/secrets/sessions \
  --proxy-map-file /var/lib/a6-trader/secrets/proxy-map.json \
  --pool-file /var/lib/a6-trader/secrets/withdraw-account-pool.json
```

确认 `shortfall=0` 后增加 `--apply`。分配对商户和地址都唯一，创建后会按稳定 Remark 回读，
重复运行执行 `noop`；已有 A6 提现账户登记为 `existing_unmanaged`，不会重复创建。该命令固定
返回 `withdrawal_submitted: false`，只准备账户，不提交提现。日常资金优先使用立即到账的商户
余额转用户余额；加密货币账户用于耗时更长的实际出金路径。

### Provider 余额补款

A6 只调度用户余额不低于一个 `quota_per_unit`（$1）的商户，而收益先落在商户可提现余额上，
两者之间需要显式划转。没有定时补款时 Provider 会把余额花光并被永久排除，实测曾出现
12 个 Provider 中 10 个报 `provider_user_balance_below_one_usd`、只剩 2 个有效 Pin。

```bash
.venv/bin/a6api-auto trade-topup-providers-once \
  --merchant 5 --merchant 7 --merchant 19 \
  --proxy-map-file /var/lib/a6-trader/secrets/proxy-map.json \
  --floor-units 2
```

默认只报告计划，确认后再加 `--apply`。`--floor-units` 以 `quota_per_unit` 为单位，默认 2
即 $2，比 $1 门槛多留一个单位余量，避免划转后立刻又跌破。缺口按整单位向上取整。

`withdrawable` 不足时该 Provider 报 `awaiting_settlement` 并保留职责，不会被移出车队 ——
收益到账有结算延迟，缺钱不等于故障。命令复用
`a6_balance_transfer_intents` / `a6_balance_transfer_confirmations`，先落意图再执行再回读
确认；发现在途意图时只做回读对账，**不会重发同额划转**。

### `us_cloud` 部署顺序

内部 Provider、relay 与 Listing 健康探测固定使用 `a6.a6api.com`。该域名一直直连
`45.61.235.131`，没有发生切源；`api.a6api.com` 昨天仍经过 Cloudflare，现已切到同一
源站，可继续作为外部入口。部署后重建连接池用于清理此前访问 `api.a6api.com` 留下的旧
Cloudflare 连接与 DNS 状态，并不表示 `a6.a6api.com` 曾经经过 Cloudflare。

Provider 运行时与 Trader 控制面是两条不同的网络边界：

```text
Sub2API account:
  proxy_id = null
  credentials.base_url = http://127.0.0.1:8767/a6-user/<A6_USER_ID>/v1
  loopback relay -> https://a6.a6api.com/v1   # direct, no proxy-map

A6 Trader control plane:
  Collector / Observer / Pin / Key reveal -> proxy-map.json
```

`proxy-map.json` 只用于访问 A6 控制台会话和读取 Key，不会被 Provider relay 读取，也不会
作为 Enodot Provider 账号的运行时代理。

仓库内 relay/unit 已按上述边界实现。2026-07-28 13:07 UTC 已在 `us_cloud` 完成独立 relay
release 发布：`/opt/a6-provider-relay/current` 指向
`20260728T130057Z-direct-v1`，`8767` unit 不再依赖 sing-box 或 `--proxy-map-file`；旧 unit
备份在 `/root/a6-trader-upstream-relay.service.pre-direct-20260728T130057Z`。临时端口与现网
端口的 direct shadow 对未授权 `/v1/models` 均连续 `20/20` 返回 `401`，完整响应 P50/P95 约
`465/481 ms`；切换期间两个 Sub2API 进程 PID 未变化。

1. 通过 `ssh us_cloud` 进入承载机，在版本目录安装 `.[trade]`。
2. 将会话放到 `/var/lib/a6-trader/secrets/sessions`，目录权限设为 `0700`、文件设为 `0600`；
   固定代理映射和 systemd `EnvironmentFile` 也设为 `0600`。
3. 发布时先运行 `trade-migrate`，再用部署会话目录运行一次 `trade-import-sessions`。已经应用的
   SQL 文件受 checksum 保护，后续结构变更应新增 migration 文件。
4. 用 systemd timer 每 5 分钟执行一次 `trade-collect-once`，并始终传入部署代理映射文件。
   另启用 `a6-trader-session-refresh.timer`，以 8 workers 按 7 分钟基础间隔和最多 3 分钟随机延迟
   刷新会话。
5. 将 `trade-dashboard` 作为常驻服务绑定 `127.0.0.1:8765`，由 Caddy 代理面板路径；不要把
   WSGI 端口直接暴露到公网。
6. 迁移/Collector 使用可写 `a6_trade` Schema 的服务角色；面板后续可拆为只读数据库角色。
   对现有 `public.usage_logs` 只授予 `SELECT`，缺少该权限时面板仅省略 Enodot 销售额。

发布目录内对应的命令为：

```bash
.venv/bin/a6api-auto trade-migrate
.venv/bin/a6api-auto trade-import-sessions \
  --sessions-dir /var/lib/a6-trader/secrets/sessions
.venv/bin/a6api-auto trade-refresh-sessions-once \
  --sessions-dir /var/lib/a6-trader/secrets/sessions \
  --proxy-map-file /var/lib/a6-trader/secrets/proxy-map.json \
  --max-workers 8 \
  --compact-output
.venv/bin/a6api-auto trade-collect-once \
  --sessions-dir /var/lib/a6-trader/secrets/sessions \
  --proxy-map-file /var/lib/a6-trader/secrets/proxy-map.json
.venv/bin/a6api-auto trade-dashboard \
  --sessions-dir /var/lib/a6-trader/secrets/sessions \
  --host 127.0.0.1 --port 8765
```

两个试水 Provider 的绑定入口为 `scripts/bind_trial_providers.py`。它默认 dry-run，只有
`--apply` 才会揭示并原子保存 A6 Key、写入控制审计和创建 Enodot 账号。新账号先以空分组和
`schedulable=false` 创建，确认摘流后才加入 `ToA6API` 分组；完成安全 Pin 前仍保持不可调度。

当前 `us_cloud` 部署入口为
[https://ai.enodot.shop/a6-trader/](https://ai.enodot.shop/a6-trader/)。Caddy 使用独立的
HTTP Basic Auth，用户名为 `a6admin`；密码保存在本机权限为 `0600` 的
`~/.config/a6api-auto/trade-dashboard-password.secret`，不复用 Enodot Admin Key。
展开商户后可按需读取 A6 登录凭据；列表接口不读取会话文件，凭据响应禁止缓存，收起商户或
点击遮罩后会从页面状态移除。Dashboard 的 `--sessions-dir` 必须显式指定为 `0700` 目录，
其中会话 JSON 必须为同一所有者的普通 `0600` 文件。
对应服务包括 `a6-trader-dashboard.service`、`a6-trader-collector.service`、
`a6-trader-collector.timer`、`a6-trader-session-refresh.service` 和
`a6-trader-session-refresh.timer`，可复现配置位于 `deploy/systemd` 与 `deploy/caddy`。刷新
service 使用 nonblocking `flock`；退出码 `2`（部分账号失败）和 `75`（锁已占用）不会把 timer
标记为 failed，详细计数仍保留在 journal。

根盘预算还包括 `deploy/systemd/journald-a6-disk-budget.conf` 和
`deploy/logrotate/postgresql-a6-disk-budget` 和
`deploy/logrotate/a6-trader-provider-pool-shadow`：journal 最多使用 200 MB 并至少保留 2 GB
空闲；PostgreSQL 日志每天轮转、单文件检查阈值 100 MB、保留 7 份压缩日志。
Provider pool shadow 另以 `0600` JSONL 保留 journal-compatible 门禁证据，每日轮转并保留
7 份，避免 200 MB journal 在 24 小时验收前丢失早期 observation。

### `hk_cloud` 双区评估

2026-07-29 的首轮 20 次未授权新连接探测中，两地均通过域名和 TLS SNI 直连
`45.61.235.131` 并返回 `401`；US 完整响应 P50/P95 约 `630/731 ms`，HK 约
`310/5377 ms`。HK 当时的 P95 来自 resolver 丢包。修复 resolver 并发布 pooled relay 后，
HK DNS 探测 `200/200`、P95 `42.929 ms`，loopback pooled relay `1000/1000` 返回预期
`401`、P95 `103.939 ms`、零错误。内部 Sub2API/relay/probe 始终固定使用
`https://a6.a6api.com`；这个域名从始至终都直连 `45.61.235.131`，没有经过 Cloudflare，
也没有发生切源。

HK 当前 loopback shadow release 为
`/opt/sub2api-hk/releases/20260729T190705Z-v159-ledger-request-id`，ELF SHA-256 为
`70bc2ef36cd4bb49d5aa0522abe1314bc6507edfec8767ec7f2e41debd8eb265`；migration 192、
prepared owner fencing 和 durable request-ID 账本已经完成真实验证，production-ledger 样本
`1/1` 通过。当前阻断项已经不是 A6 网络路径，而是 HK 经 SSH state tunnel 访问 US Redis：
10 次 RTT P50/P95 为 `337.007/630.440 ms`，US 本地 100 次仅为
`0.074/0.111 ms`。因此 A 到 B 的 dispatch 仍约 `509-796 ms`，超过 `<250 ms p95`
门禁。

当前继续由 US 承载 active request/control plane，HK 只监听 loopback，LB 权重固定为 `0`。
双区前必须把 Redis/dispatch 协调改成区域内热路径或区域化协调架构，不能继续让请求热路径穿过
SSH tunnel；随后再跑 strict A-to-B、24 小时稳定性和 1-5% 连接粘性灰度。完整证据与命令见
`docs/a6-provider-topn-failover-design-20260728.md` 的 22.3 节。

### Provider 错误短路

- HTTP `403` 且包含 `账户额度不足`、`账户余额或订阅额度不足`、`额度不足，请充值`，或
  额度不足错误码时，分类为 `account_quota_insufficient`。抓到的 `consecutive_403=3/3`
  表示连续错误已达短路阈值。
- 该分类固定短冷却绑定的 Enodot Provider 60 秒，当前请求切换下一个 Provider，后台按
  `balance -> transfer_policy -> pin` 诊断。
- 客户端只接收 `provider_pool_temporarily_unavailable`；A6 原始正文、Request ID、渠道、商户
  和连续错误计数都不透出。
- 普通 HTTP `401`，以及经接口语义确认的认证型 HTTP `403`，另走 Session 刷新或 A6 Key
  校验路径，不触发余额划转或 Pin 切换。`/api/supplier/me` 的供应商状态型 `403` 也单独处理。

## Validate without mutations

```bash
.venv/bin/a6api-auto validate-templates

.venv/bin/a6api-auto run \
  --qq-file /absolute/path/to/qq-100.txt \
  --upstream-base-url https://UPSTREAM.example/v1 \
  --upstream-api-key-env A6_UPSTREAM_API_KEY
```

The second command reports readiness unless `--live` is present.

## Run

先在进程环境中提供 `A6_UPSTREAM_API_KEY`，再运行：

```bash
.venv/bin/a6api-auto run --live \
  --qq-file /absolute/path/to/qq-100.txt \
  --upstream-base-url https://UPSTREAM.example/v1 \
  --upstream-api-key-env A6_UPSTREAM_API_KEY \
  --state-dir ~/.config/a6api-auto
```

Optional flags:

- `--email ADDRESS`: select one exact address from the QQ pool instead of random selection.
- `--template-id merchant-001`: select one exact template instead of random selection.
- `--proxy-file FILE`: randomly select one proxy from an owner-only JSON array or line list.
- `--upstream-api-key-file FILE`: read the upstream key from a `0600` file.
- `--session FILE`: resume an interrupted full workflow from an existing session file.

Direct `--upstream-api-key` input is supported for module composition, while environment or owner-only file input avoids placing it in shell history.

## Slow batch of 20 accounts

The batch accepts either a direct proxy pool or an owner-only (`0600`) sing-box subscription list. Direct files support `http`, `https`, `socks5`, and `socks5h`. Subscription mode downloads each sing-box JSON with a sing-box User-Agent, keeps one local mixed/SOCKS instance per link, and routes through the largest built-in urltest group. Each attempt draws from a shuffled local proxy deck and avoids immediate reuse when possible.

The bundled upstream catalog contains the 30 requested domains. In the default batch mode, each account receives a random domain and one random active API Key from the exact `TPA6API` group on `ai.enodot.shop`. The Enodot admin credential is read from `AI_ENODOT_SHOP_ADMIN_KEY`; only the selected Key ID and domain are persisted, while both the admin credential and selected Key value stay out of batch state and logs.

Preview inputs without registering anything or reading the admin credential:

```bash
.venv/bin/a6api-auto batch \
  --qq-file /absolute/path/to/qq-100.txt \
  --proxy-subscription-file ~/.config/a6api-auto/proxy-subscriptions.secret \
  --proxy-subscription-insecure \
  --count 20 \
  --min-interval-minutes 180 \
  --max-interval-minutes 480
```

Set the Enodot admin credential and add `--live` to start the serial batch:

```bash
.venv/bin/a6api-auto batch --live \
  --qq-file /absolute/path/to/qq-100.txt \
  --proxy-subscription-file ~/.config/a6api-auto/proxy-subscriptions.secret \
  --proxy-subscription-insecure \
  --count 20 \
  --min-interval-minutes 180 \
  --max-interval-minutes 480 \
  --enodot-admin-url https://ai.enodot.shop \
  --enodot-admin-key-env AI_ENODOT_SHOP_ADMIN_KEY \
  --enodot-group TPA6API
```

The first JSON event prints the generated batch state path. Stop with `Ctrl-C` at any time and resume with the same inputs plus that path:

```bash
.venv/bin/a6api-auto batch --live \
  --batch-state ~/.config/a6api-auto/batches/batch-YYYYMMDD-HHMMSS-RANDOM.json \
  --qq-file /absolute/path/to/qq-100.txt \
  --proxy-subscription-file ~/.config/a6api-auto/proxy-subscriptions.secret \
  --proxy-subscription-insecure \
  --count 20 \
  --min-interval-minutes 180 \
  --max-interval-minutes 480 \
  --enodot-admin-url https://ai.enodot.shop \
  --enodot-admin-key-env AI_ENODOT_SHOP_ADMIN_KEY \
  --enodot-group TPA6API
```

Use `--upstream-domain-file FILE` to replace the bundled domain catalog with a JSON array or line list. To retain the fixed-upstream mode, pass `--upstream-base-url` together with exactly one of `--upstream-api-key`, `--upstream-api-key-env`, or `--upstream-api-key-file`.

`--proxy-file FILE` retains direct-proxy mode. In subscription mode, cached sanitized configs live under `~/.config/a6api-auto/sing-box` with owner-only permissions. `--proxy-subscription-insecure` affects only subscription downloads; a validated cache is used when a later refresh fails. The two configured subscription endpoints currently require this flag during their TLS handshake.

Twenty successful accounts have 19 normal gaps, so the baseline runtime is 57–152 hours. Failed attempts and retries add further delays. The next run time is persisted before sleeping, and a restart waits only the remaining portion of the same interval. Batch and session files are `0600` under `0700` directories; batch state stores only proxy fingerprints, upstream domains, and Enodot Key IDs, never proxy URLs, admin credentials, or full upstream keys.

## Refresh an expired session

```bash
.venv/bin/a6api-auto refresh-session \
  --session ~/.config/a6api-auto/sessions/session-HASH.json
```

The command first calls `/api/user/self`. An expired cookie triggers password login and an atomic session rewrite. The command prints the session path, user ID, and cookie names; secret values remain in the `0600` session file.

To resume the remaining Key/model/supplier stages as well, run the normal `run --live` command with
`--session SESSION_FILE` and the upstream parameters.

## Session format

Session files are JSON with mode `0600` under a `0700` directory. They contain the selected QQ address, generated username/password, cookie values, user ID, one a6api key, selected template ID, upstream model names, and supplier request IDs. The full upstream supplier key is sent in the application request but is not persisted; only its last four characters are retained for reconciliation.
