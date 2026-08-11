# Phase 1 试水落地记录（2026-07-25）

## 当前状态

- 承载机：`us_cloud`
- 根盘：29 GB，总使用率 83%，可用约 5.0 GB
- Dashboard：`https://ai.enodot.shop/a6-trader/`
- Collector：每 5 分钟；本次验收 `16 collected / 0 failed / 3 skipped`
- Provider 账号均已绑定，但因尚无安全 Pin，保持 `schedulable=false`

## 首批 Seller 与运行职责

19 个商户全部具有 Seller 基础身份；下面的 Provider / Observer 是可切换运行职责。切换职责
不会下架该商户的受管渠道或 Listing。尚未通过供应商申请的账号保持待就绪 Seller，申请通过
后再创建渠道和 Listing。

| 角色 | Merchant | A6 user | 说明 |
| --- | ---: | ---: | --- |
| Provider duty | 5 | 9517 | Seller；Token 14020，Enodot account 45895 |
| Provider duty | 19 | 9812 | Seller；Token 14093，Enodot account 45896 |
| Seller-only | 4 | 7775 | 活跃 Seller |
| Seller-only | 7 | 8085 | 近期开单 Seller |
| Seller-only | 6 | 9727 | 已回读 Listing channel 1140 |
| Observer duty | 1 | 9645 | Seller；独立代理出口 |
| Observer duty | 2 | 9627 | Seller；独立代理出口 |
| Observer duty | 3 | 9760 | Seller；独立代理出口 |

角色事务首次产生 8 条 `assign_merchant_role` 审计。使用相同配置重复执行为 0 change、
0 new audit。

## Provider Binding

| Merchant | Binding UID | Enodot proxy | 状态 |
| ---: | --- | ---: | --- |
| 5 | `a6:merchant-5:token-14020` | 5 / port 23084 | bound, disabled |
| 19 | `a6:merchant-19:token-14093` | 18 / port 23096 | bound, disabled |

共同配置：

- `base_url=https://a6.a6api.com/v1`
- `model_mapping={"gpt-5.6-sol":"gpt-5.6-sol"}`
- `group_ids=[26]` (`ToA6API`)
- concurrency 1、priority 1、rate multiplier 1
- 完整 Key 只在对应 `0600` Session 中保存；数据库和 Enodot extra 只保存 SHA-256

两次创建均完成 API 和数据库双回读。第二次 apply 均返回 `noop`，没有新账号、Session 写入或
新增 attempt。两个账号创建后用量记录为 0。

## 自有渠道排除集

本次通过 16 个提交商户的 `/api/supplier/me/channels` 回读得到 17 个组织自有渠道：

```text
768, 832, 1087, 1100, 1101, 1104, 1107, 1108, 1112,
1120, 1129, 1140, 1148, 1149, 1158, 1160, 1162
```

该集合已写入 `snapshot_owned_channels` 控制审计。任何 Provider Pin 候选必须先排除整个集合，
而不只是当前三个 Seller 的渠道。

## 经营利润

确认利润口径：

```text
Enodot 销售 - 已绑定 Provider 客户成本
  + 自然 Seller 收益 - Probe 净损耗 - 提现手续费
```

历史 Seller 收益已通过可续传脚本幂等回填：27,641 条事件合计 11.946244，与
`a6_financial_current.lifetime_earned_micros` 合计一致，差额为 0。Binding 前的 Provider 成本
0.086406 单列为待归属，因此全量利润状态当前为 `partial`；账号开始承接客户流量后，销售和
Provider 成本只从 Binding 生效时间以后进入确认账。

## 磁盘预算

- 删除 7 个无进程引用的 `/tmp/sub2api-0.1.159-sg-adjustment.*` 副本，回收约 799 MB
- journal 上限 200 MB，至少保留 2 GB 根盘空间，最多保留 7 天
- PostgreSQL 日志每日轮转，100 MB 检查阈值，保留 7 份压缩日志
- 未重启两套 Sub2API 蓝绿实例；仍被旧进程持有的 126 MB 删除日志留待维护窗口处理

Provider 启用前仍需完成：余额策略、排除自有渠道后的外部 Pin、Pin 回读，以及首个小流量请求
的 Enodot/A6 双账核对。

## 金额与提现账户补充

- Session `quota_per_unit` 当前为 `500000`，即 `$1 = 500000 A6 quota`。
- 商户余额转用户余额最低 `$1` 且立即到账；控制器按每个商户的汇率生成下限并先做 Preview。
- 加密货币提现耗时更长，独立维护私有 `(network, address)` 池，每个商户和地址均唯一绑定。
- 账户池控制器只创建/登记账户，不自动提交加密货币提现。
