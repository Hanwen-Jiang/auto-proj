# A6 / Enodot 买卖系统：框架层评审

日期：2026-07-26
配套文档：[接管诊断与重新设计](a6-trader-takeover-review-20260726.md)（业务口径与 P0 修复）

上一份文档修的是**具体的错**。这一份分析的是**为什么这些错会发生，而且注定会再发生一次**。

---

## 0. 一页总结

### 0.1 核心判断

前一份文档说「所有闸门都是单向的」。那是症状。框架层的病根是：

> **系统只有「执行器」，没有「控制平面」。**
> 5 个 systemd timer 各自驱动一个「做一次某件事」的命令行脚本，没有任何组件持有
> 「期望状态」并负责把现实收敛过去。

一个只会反应、不会断言的系统，其闸门**必然**是单向的 —— 因为没有任何地方记录着
「这个策略本该是开的」。退休流程关掉增长不是疏忽，是这个架构下的正常产物。

### 0.2 十个框架问题（按风险排序）

| # | 问题 | 关键证据 | 严重度 |
| :--- | :--- | :--- | :--- |
| F1 | 无控制平面，无期望状态，无 reconciler | 无 kill switch；`paused`/`halt` 零匹配 | 致命 |
| F2 | **SQL 层零执行覆盖** | 663 个测试全是 fake / 字符串断言；12 个 Postgres store 有 3 个零测试引用 | 致命 |
| F3 | 配置写在 systemd 单元参数里 | 3 个单元共 **56 个 `--` 调参标志**；改 `--margin` 要发版 | 高 |
| F4 | 审计分裂成 5 张互不相通的表 | 回答「1 小时内对 merchant 9 做了什么」需要 union 5 张表 | 高 |
| F5 | `cli.py` 是 god object | 1878 行，168 个 `add_argument`，扇出 **26** 个模块 | 中高 |
| F6 | 巨型模块与巨型 SQL | 5 个模块 > 2000 行；`_MERCHANT_QUERY` 约 400 行 SQL / 8 个位置参数 | 中高 |
| F7 | 两份 `migrations/` 目录靠手工同步 | 本次新增 0021 必须 `cp` 两次，无校验 | 中 |
| F8 | 时序与并发是手工凑的 | `a6-trader-pin` 是唯一**既用错 timer 指令又没有锁**的服务 | 中 |
| F9 | 无效果度量层 | 动作有记录，结果无记录，系统能行动不能学习 | 中（P1 已设计） |
| F10 | 事故写多读少 | `trade_incidents`：2 个写入方，1 个读取方，面板 **0 处引用** | 中 |

### 0.3 本次评审顺手修的真 bug

`trade_provider_topup.PostgresProviderTopupStore.record_failure` 向
`a6_trade.trade_incidents` 写入 `error_class` —— **该表没有这个列**，只有
`incident_class NOT NULL`。运行时必然抛错。

而 663 个测试**全绿**，因为 store 只被 fake 测过，那段 SQL 从未执行。这就是 F2 的活体样本。
已修复，并新增 [tests/test_sql_schema_contract.py](../tests/test_sql_schema_contract.py)：
解析全部 migration 的 DDL，交叉校验源码里每一条 `INSERT` 的列名。全库扫描后**只有这一处
不一致**，说明其余 SQL 是干净的。

---

## 1. F1：没有控制平面

### 1.1 现状

```text
systemd timer ──> a6api-auto <一次性命令> --apply ──> A6 / Enodot
     (5 个)              (16 个子命令)
```

每个命令的形状都是「读现状 → 算一个动作 → 做掉 → 退出」。没有任何地方存着
「期望状态」，所以：

- 没法回答「现在的配置对不对」，只能回答「刚才那一次做了什么」。
- 任何把状态改坏的写入（比如 `enabled=false`）都没有对手方把它改回去。
- 没有全局开关。实测 `kill_switch` / `global_pause` / `emergency` / `halt` 在
  源码和 migration 里**零匹配**。5 个 timer 各自能动钱、能改 A6 上架状态，
  出问题时只能 `systemctl stop` 五次，且在途的那一轮还会跑完。

### 1.2 期望状态缺失的具体代价

前一份文档的三个 P0 问题，全部是同一个结构缺陷的不同表现：

| 症状 | 缺失的期望状态 |
| :--- | :--- |
| `callback_enabled=false` 覆盖 16/16 且无人重开 | 「这条 listing 的增长策略应该是开的」 |
| 10 个 Provider 跌破 $1 被永久排除 | 「Provider 的用户余额应该 ≥ $2」 |
| 价格上限长期停在 0.30 | 「模型的价格上限应该是 1.0」 |

我这次是**逐个打补丁**修掉的（退休不再关闭、新增补款命令、上限待调）。这能让系统活过来，
但下一个「某个流程把某个开关关了」还会重演。

### 1.3 目标形态

```text
                   ┌──────────────────────────┐
   面板 / CLI ────> │  desired_state（唯一真相）│
                   └────────────┬─────────────┘
                                │
                        ┌───────▼────────┐
                        │   reconciler   │  单进程，串行，可暂停
                        └───────┬────────┘
             diff(desired, observed) ──> 动作队列
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
   pin actuator          price actuator         growth actuator
        └───────────────────────┴───────────────────────┘
                                │
                         A6 / Enodot（写）
                                │
                         collector（读）──> observed_state
```

三条硬规则：

1. **执行器不做决策。** actuator 的签名是
   `(desired, observed) -> list[Action]`，纯函数，可单测，不碰网络。
   现在的 `trade_pin_runner` 等模块就是执行器，只是决策和 IO 混在一起。
2. **任何「关」都必须带 TTL 或写入 desired_state。** 不允许再出现无对手方的
   `SET enabled = false`。P0-B 引入的 `suppressed_until` 是这条规则的第一个实例，
   应推广到全部开关。
3. **全局 `paused` 是 desired_state 的一个字段。** reconciler 每轮开头检查，
   一处生效，无需停 5 个 timer。

### 1.4 迁移路径（不重写）

30k 行不可能推倒。用 strangler：

| 步骤 | 动作 | 风险 |
| :--- | :--- | :--- |
| 1 | 建 `trade_desired_state` 表 + `paused` 全局开关 + reconciler 骨架，**只读、只报告 diff** | 无 |
| 2 | 把现有 56 个 systemd 调参标志导入 desired_state（F3 一并解决） | 无 |
| 3 | reconciler 接管**一个**执行器（建议从 growth 开始，它已有 `claim_action` 幂等键） | 低 |
| 4 | 逐个迁移 pin / price / topup，每迁一个就删掉对应 timer | 中 |
| 5 | 5 个 timer 收敛为 1 个 reconciler 进程 | 中 |

第 1、2 步就能拿到大部分价值：可见的期望状态 + 一个全局刹车。

---

## 2. F2：SQL 层零执行覆盖（最高即时风险）

### 2.1 现状

| 指标 | 实测 |
| :--- | :--- |
| 测试总数 | 663，全绿 |
| 真正执行 SQL 的测试 | **0** |
| DB 相关测试的形态 | 对 migration 文本做 `assert "CREATE TABLE ..." in migration` |
| Postgres store 类 | 12 个 |
| 零测试引用的 store | 3 个（`PostgresChannelGrowthStore`、`PostgresProviderTopupStore`、`PostgresSellerRetirementStore`） |
| 其余 store 的引用数 | 多为 1（仅构造，未执行） |

所有 store 测试都注入 fake connection，因此：

- 列名写错 → 绿
- 表名写错 → 绿
- `ON CONFLICT` 目标不存在 → 绿
- 参数个数与占位符不匹配 → 绿（我改 `_MERCHANT_QUERY` 时确实撞上这一条，靠手数 `%s` 发现）
- 事务边界写错 → 绿

**这是一个搬钱的系统。** `a6_balance_transfer_intents`、`balance_transfer` 这条链路上的
SQL 从未被执行过一次。

### 2.2 已补的第一层：静态契约

[tests/test_sql_schema_contract.py](../tests/test_sql_schema_contract.py) 重放全部 migration 的
`CREATE TABLE` / `ADD COLUMN` / `DROP COLUMN`，再校验源码中每条 `INSERT` 的列名。

它捕获列名/表名类错误，成本为零（0.04s，无需数据库）。**它不能**捕获参数个数、
`ON CONFLICT` 目标、事务语义。

同时加了一条自检 `test_the_parser_actually_found_the_schema`，防止解析静默失败导致
校验空转 —— 否则这个测试自己就会变成又一个「永远绿」的装饰。

两个守卫都**反向验证过**（这类静态检查最容易写成永远通不了失败的装饰）：

| 守卫 | 注入的缺陷 | 结果 |
| :--- | :--- | :--- |
| 列名契约 | 把 `incident_class` 改回 `error_class` | 失败，且报出 `trade_provider_topup.py: a6_trade.trade_incidents.error_class` |
| 目录一致性 | 向 `data/migrations/0021` 追加一行注释 | 失败 |

同时补上了 `test_both_migration_directories_are_byte_identical`（FW-0 的另一半，见 §6）。

### 2.3 需要补的第二层：真库往返

建议引入一个真实 PostgreSQL 测试夹具（本地 PG 或 testcontainers），做：

1. `apply_migrations` 全量跑通，**并且能重复跑**（幂等 + checksum 保护）。
2. 每个 store 的写-读往返：`begin_transfer` → `load_pending_intent` → `confirm_transfer`
   拿回同一份数据。
3. `_MERCHANT_QUERY` 在空库和有数据两种情况下都能执行（8 个参数、约 400 行 SQL，
   现在完全没有执行验证）。
4. 关键不变量的数据库级验证：`counterparty_class` 的 CHECK、
   `a6_earning_events_merchant_id_a6_earning_id_key` 的去重、
   余额划转确认的唯一性。

优先级：**这应该排在 P1 之前**。放开抖加和回收会增加写操作频率，而写路径目前没有执行覆盖。

---

## 3. F3：配置在 systemd 单元参数里

实测三个部署单元的 `ExecStart` 共 **56 个 `--` 标志**。样本：

```text
--margin 0.20 --conversion-factor 0.833333333333 --price-headroom 0
--maximum-price-multiplier 0.4 --maximum-final-multiplier 0.4
--maximum-band-utilization 0.75 --quote-max-age-seconds 300
--usage-window-seconds 3600 --top-k 5 --min-observers 2
--probe-budget 8 --probe-failure-ttl-seconds 45 ...
```

后果：

- 改一个利润底线要：编辑单元 → `daemon-reload` → `restart`，**且无审计记录**。
  谁在什么时候把上限设成 0.4 的，查不到。
- 这直接解释了为什么价格上限长期停在 `0.4 × 0.75 = 0.30` —— 调它的成本等于发一次版。
- `--config-version` 字段存在且写入审计，但它记录的是**代码版本意图**，不是参数值。

**修法**：`trade_config` 表（key / value / valid_from / changed_by / reason），
面板可编辑，每次变更写审计。单元参数只保留身份与秘密路径（`--sessions-dir`、
`--proxy-map-file`、`--enodot-admin-key-file`）。

这一步和 F1 的第 2 步是同一件事，应合并做。

---

## 4. F4：审计分裂成 5 张表

| 表 | 写入方 | 覆盖 |
| :--- | :--- | :--- |
| `trade_control_actions` + `_attempts` | 11 个模块 | 通用控制动作 |
| `a6_channel_growth_actions` | growth | 回收 / 抖加 / readiness |
| `a6_balance_transfer_intents` + `_confirmations` | retirement, topup | 资金移动 |
| `a6_seller_retirement_events` | retirement | 退休状态机 |
| `a6_channel_health_events` | pin / observer | 健康跃迁 |

每张表**单独看都设计得不错**（幂等键、意图/确认分离、状态机）。问题是没有统一时间线：
「过去 1 小时对 merchant 9 做了哪些写操作」需要 union 5 张表并手工对齐时间戳。

对一个**病根就是「没人发现某个策略死了」**的系统，这是关键缺陷。

**修法**（低成本）：建一个 `trade_activity` 视图 union 五个来源，统一
`(occurred_at, entity_type, entity_id, actor, action, outcome, ref)`，面板加一条时间线。
不需要改写入方。

---

## 5. F5 / F6：结构性代码问题

### 5.1 `cli.py` 扇出 26 个模块

```text
cli -> batch, client, domains, enodot, enodot_admin, errors, mail, singbox,
       storage, templates, trade_channel_growth, trade_collector, trade_dashboard,
       trade_db, trade_enodot_seller_resources, trade_managed_listing_store,
       trade_managed_listings, trade_pin_runner, trade_provider_topup,
       trade_repository, trade_roles, trade_seller_orchestration,
       trade_seller_price_band, trade_seller_retirement, trade_withdraw_accounts,
       workflow
```

1878 行，168 个 `add_argument`。所有依赖装配都在这里。加一个命令要改 3 处
（parser / handler / dispatch），我这次加 `trade-topup-providers-once` 就改了 3 处。

好消息：依赖是**单向的**，没有环。`errors`（扇入 22）和 `storage`（扇入 15）是干净的共享内核。

**修法**：命令注册表 —— 每个命令一个模块，导出 `NAME / add_arguments / run`，
`cli.py` 只做发现与分发。可以增量做，不必一次全搬。

### 5.2 巨型模块

| 模块 | 行数 |
| :--- | ---: |
| `trade_managed_listings.py` | 2597 |
| `trade_pin_runner.py` | 2540 |
| `trade_repository.py` | 2530+ |
| `trade_managed_listing_store.py` | 2224 |
| `trade_seller_price_band.py` | 2018 |

`trade_repository.py` 一个类同时承担三种职责：collector 持久化、面板查询、定价带查询。
`_MERCHANT_QUERY` 约 400 行 SQL 带 8 个**位置参数**（全是同一个 `since`），
加字段要同步改 4 个地方 —— 我改的时候就是靠脚本数 `%s` 才发现参数个数不对。

**修法**（按性价比排序）：

1. **SQL 位置参数改命名参数**（`%(since)s`）。改动小，直接消除这一类错误。
2. 按职责拆 `PostgresTradeRepository`：`CollectorSink` / `DashboardQueries` / `PriceBandQueries`。
3. 其余巨型模块暂不动 —— 它们内部有清晰的 Protocol 分层，收益不抵风险。

### 5.3 `trade_seller_retirement` 依赖 `trade_pin_runner`

退休流程 import 2540 行的 pin runner。这是可疑耦合，值得确认是否只为了少数几个符号；
如果是，应下沉到共享模块。

---

## 6. F7：两份 migrations 目录

```text
migrations/0021_....sql                       ← 源码检出时使用
src/a6api_auto/data/migrations/0021_....sql   ← 安装为包时使用
```

`trade_db.default_migration_directory()` 优先用前者，回退到后者。两份内容必须一致，
但**没有任何校验**。本次新增 0021 时我必须手工 `cp`，忘了就会出现「本地测试通过、
部署后缺列」。

**修法**：三选一 —— 打包时构建步骤复制、符号链接、或加一个测试断言两目录逐字节一致。

已采用第三个（最便宜）：`test_both_migration_directories_are_byte_identical`，
比对文件名集合与逐字节内容。已反向验证：向任一目录追加一行即失败。

---

## 7. F8：时序与并发

| 服务 | timer 指令 | 锁 | 评价 |
| :--- | :--- | :--- | :--- |
| collector | `OnCalendar=*:0/5:00` | 无 | 可接受（只读，5 分钟间隔远大于 3s 运行时） |
| **pin** | **`OnUnitActiveSec=10s`** | **无** | **两处都有问题** |
| channel-growth | `OnUnitInactiveSec=10s` | `seller-control.lock` | 正确 |
| seller-pilot | `OnUnitInactiveSec=10s` | `seller-control.lock` | 正确 |
| seller-retirement | `OnUnitInactiveSec=60s` | `seller-control.lock` | 正确 |

两个具体问题：

1. **pin 用的是 `OnUnitActiveSec`，别人用 `OnUnitInactiveSec`。**
   前者从「服务开始运行」起算，后者从「服务结束」起算。pin 实测单次运行 >8s，
   所以 `OnUnitActiveSec=10s` 只留 ~2s 间隙；一旦某轮超过 10s，systemd 会在上一轮
   仍在运行时尝试启动下一轮。而 pin **是唯一没有 flock 的写服务**，
   并发 pin 会争抢同一个 token 的 Pin 绑定和 Enodot 调度参数。
   → 改成 `OnUnitInactiveSec=30s` + 独立 flock。

2. **三个服务共用一把 `seller-control.lock`。**
   growth（10s）、pilot（10s）、retirement（60s）互相串行。这可能是有意的
   （都改 seller 资源），但也意味着一个慢的 pilot 会饿死 growth。
   → 确认是否有意；若无意，按资源粒度拆锁。

F1 的目标形态（单 reconciler 进程串行执行）会一次性消除这整类问题。

---

## 8. F10：事故写多读少

`trade_incidents` 结构完善（`incident_key` 唯一、`occurrence_count`、`severity`、
`status`、`resolved_at`），但：

- 写入方：2（`trade_repository`、`trade_provider_topup`）
- 读取方：1（`trade_repository.py:2397`，只读 `incident_class` 和 `last_seen_at`）
- **面板对 `incident` 零引用**

所以事故被规范地记录下来，然后没人看。对照 F4：这个系统的失败模式就是「安静地退化」，
而唯一为此设计的表没有出口。

**修法**：面板加事故面板（按 severity / status / 实体聚合），并对
`own_pin_leak_detected`、`provider_topup_transfer_unconfirmed`、
`awaiting_settlement` 持续超过 N 分钟这三类配告警。

---

## 9. 明确应当保留的架构决策

这些是原设计做对的部分，重构时必须保留：

| 决策 | 为什么对 |
| :--- | :--- |
| 事实与控制分表（`*_events` / `*_current` vs `trade_control_*`） | 逐请求事实是唯一能重算 P&L 的东西，实测证明了这一点 |
| 意图 → 执行 → 回读确认 | 响应丢失时靠观测效果对账，而不是重发。资金安全的核心 |
| 每个写命令 preview / `--apply` 双模 | 我这次所有验证都靠它，零风险 |
| 秘密不入库，`0600`/`0700`，面板不接触凭据 | 一致执行，无例外 |
| A6 原始整数入库，按 `quota_per_unit` 逐商户换算 | 避免了跨商户汇率错误 |
| 幂等键覆盖全部写动作 | 重复执行安全，本次多次重跑命令都是 noop |
| Collector 的 `upstream_mutations: false` 契约 | 采集永不写上游，这个边界很干净 |

**不要因为 F5/F6 去重写这些。** 框架问题在编排层和测试层，不在领域建模层。

---

## 10. 建议顺序

按「风险 ÷ 成本」排序，与业务 P1 交错进行：

| 优先级 | 动作 | 成本 | 说明 |
| :--- | :--- | :--- | :--- |
| ~~FW-0~~ | ~~INSERT 列名 / 表名静态契约~~ | 已完成 | 捕获并修复了本次引入的 `error_class` bug |
| ~~FW-0~~ | ~~两份 migrations 目录一致性测试~~ | 已完成 | 已验证：改动任一目录即失败 |
| **FW-0** | pin 服务补 flock + 改 `OnUnitInactiveSec=30s` | 30 分钟 | 已单独立项 |
| **FW-1** | 真库测试夹具 + 12 个 store 的写-读往返 | 1–2 天 | **排在业务 P1 之前**，因为 P1 会提高写频率 |
| **FW-2** | `trade_config` 表 + 56 个标志入库 + 面板可调 | 1 天 | 同时解决 F3 和 F1 第 2 步 |
| **FW-3** | `trade_desired_state` + `paused` 全局开关 + reconciler 骨架（只读报告 diff） | 1 天 | F1 的最小可用形态 |
| **FW-4** | `trade_activity` union 视图 + 面板时间线 + 事故出口 | 0.5 天 | 解决 F4 和 F10 |
| **FW-5** | SQL 位置参数改命名参数 | 0.5 天 | 消除一类静默错误 |
| **FW-6** | reconciler 接管 growth 执行器 | 1 天 | 第一个真正的闭环 |
| FW-7 | 命令注册表拆 `cli.py` | 1 天 | 可延后 |
| FW-8 | 拆 `PostgresTradeRepository` 三职责 | 1–2 天 | 可延后 |

**FW-0 与 FW-1 建议插到业务 P1 之前**：P1 要放开回收和抖加，这两者都增加写操作，
而写路径当前零执行覆盖。先有安全网再踩油门。
