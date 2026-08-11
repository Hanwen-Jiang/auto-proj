# 后续计划

> 状态指针（2026-08-05）：本文的 P0-1（Enodot 收入分类）、P0-2（Collector 控制区）、
> P0-3（旧名称 Seller 退休）是更早期阶段的后续清单，**与当前 Provider auto-pool /
> listing-schedulability / economic-gate 阶段目标不再平行**，正文历史内容保留，不回写。
> 当前阶段的权威目标、顺序任务、验收与 runbook 以
> [`architecture-deployment-review-20260805.md`](architecture-deployment-review-20260805.md)
> 为准；其中与本文相关的唯一延续点是 P0-3 的 Seller 身份/退休语义在 auto-pool 与
> membership retirement（迁移 `0068`）下按新合同重估。
> **目标顺序（不得跳跃，也不代表当前代码或部署已获批）**：
> 第一步先收口 P0 合同（P0-1 dispatch 跨 schema/consumer 合同、P0-2 lifecycle stop
> boundary、P0-3 Ban 可见性、P0-7 独立 owned-Channel Ban preflight 入口）并完成 T1 fresh
> live 只读预检；第二步才批准 T4 代码/unit/测试修复（分 T4-producer 与 T4-consumer 同批
> 批准，producer-only 不得进入部署）；
> 第三步在 T4 通过后再单独批准 T5 部署。在本指针写就时，相关 P0 尚未定案、生产状态
> 未做 fresh readback，**不得据此推断任何代码或部署已获批准**。本指针之外的历史计划
> 如需重开，应先在审阅稿 §11 未决清单登记。

## P0-1：修正 Enodot 收入分类与倍率口径

当前面板将 `public.usage_logs.actual_cost` 直接汇总为“Enodot 外部收入”，并把未命中内部
分类规则的流量默认视为外部客户流量。线上核对已确认该口径产生错误结果。

### 已确认的问题

- 面板显示的 `$24.573111` 来自 OpenAI 原生计价，相关记录满足
  `total_cost = actual_cost`、`rate_multiplier = 1`、`account_rate_multiplier = 1`，没有乘销售倍率。
- 这批记录全部属于 Enodot 管理员 `user_id=1 / api_key_id=54 / group_id=26`，属于内部运营
  流量，不是外部客户收入。
- 当前规则只把管理员 Key `53` 标记为内部激活流量；Key `54` 没有规则，被错误兜底为
  `external`。
- 因此“19 笔 / $4.134555 Enodot 外部收入未匹配 Provider 成本”也是错误分类后的告警，
  不能用于判断外部收入或实际利润。

### 正确账务口径

- `OpenAI 原生计价`：`usage_logs.total_cost`，只作为模型原生价格和定价基数。
- `倍率后客户扣费`：使用经过 Key、用户组、订阅及其他生效倍率后的真实扣费字段；读取时必须
  同时校验倍率快照，`rate_multiplier=1` 时明确显示为 `1x`。
- `A6 Provider 实际成本`：使用唯一匹配的 A6 Provider 消费日志及该 Session 的
  `quota_per_unit` 换算，不使用 OpenAI 原生计价代替。
- `外部客户确认收入`：只统计身份已确认为外部客户的倍率后实际扣费。
- `确认净利润`：外部客户确认收入减去唯一匹配的 A6 Provider 实际成本，再合并 Seller 收益、
  Probe 净损耗和提现手续费。
- 管理员、Owner 激活、面板创建的 managed Seller Key、探活和其他运营请求统一进入内部科目，
  不计外部收入。

### 数据修正

- 为 `user_id=1 / api_key_id=54` 建立带有效时间、原因和创建者的 `internal_operation` 规则。
- 核对 Key `54` 的创建时间和第一条用量记录，按准确有效期回算历史数据。
- 审计管理员名下其他 Key，逐个确认 `owner_activation`、`internal_operation` 或真实外部用途，
  禁止继续以“未命中规则即外部收入”作为最终账务结论。
- 重新计算受影响窗口的外部收入、待归属流水、Provider 成本和确认净利润。
- 保留修正前后的金额、请求数和规则版本，支持复核和回滚。

### 面板调整

- 将当前含糊的“Enodot 外部收入”拆分为“OpenAI 原生计价”“倍率后客户扣费”和
  “外部客户确认收入”。
- 在金额旁展示实际生效倍率及来源：Key、用户组、订阅或默认倍率。
- 内部流量单独展示请求数、原生计价、内部扣费和对应 Provider 成本。
- “外部收入未匹配 Provider 成本”只统计身份已确认的外部客户请求。
- 分类或倍率证据不完整时，确认利润保持待归属状态，不展示为已确认收益。

### 验收标准

- `user_id=1 / api_key_id=54` 的历史和新增请求均不会进入外部收入。
- 在当前已知数据快照上，原 `$24.573111` 从外部收入和确认利润中移除并进入内部运营科目。
- 构造 `1x` 与非 `1x` 用例，分别验证原生计价、倍率后扣费和 Provider 实际成本互不混用。
- 任意收入均可追溯到外部用户、API Key、倍率快照和用量日志。
- 任意 Provider 成本均可追溯到 A6 Token、渠道、Session `quota_per_unit` 和消费事件。
- 三方无法唯一匹配时只进入待归属队列，不抬高确认利润。

## P0-2：面板 Collector 控制区

在管理面板增加独立的 Collector 运维控制区，与 Provider Pin、Seller Listing 等远端写入控制
完全分离。Collector 控制仅管理只读采集任务及其调度状态。

### 操作

- 启用定时采集。
- 暂停定时采集。
- 立即采集一轮。
- 采集任务运行期间禁用重复触发，并明确显示“运行中”。

### 状态展示

- 当前运行状态：`已暂停 / 等待执行 / 运行中 / 最近失败`。
- 上次开始时间和上次完成时间。
- 下一次计划执行时间。
- 本轮成功、失败和跳过的账号数量。
- 最近错误摘要，包括商户、错误类别、发生时间和最近连续失败次数。
- 当前采集周期，以及最后一次成功快照距现在的时间。

### 后台接口与审计

- 所有控制接口仅接受面板管理员凭据，不向浏览器暴露服务器命令或系统权限。
- 使用固定动作接口实现 `enable`、`pause` 和 `run-once`，不接受任意命令参数。
- 每次操作记录操作者、动作、请求时间、执行结果和失败原因。
- `run-once` 使用互斥锁或 systemd 作业状态防止同一时刻重复采集。
- 启停 Collector 不联动 Pin、Seller Pilot 或 Listing 自动化任务。

### 验收标准

- 面板状态与服务器实际调度、任务状态一致。
- 点击“立即采集”后可看到本轮开始、完成及成功/失败/跳过数量。
- 暂停后不再出现新的定时采集轮次，手动采集仍可单独执行。
- 启用后恢复每 5 分钟一轮的调度，并展示准确的下一次执行时间。
- 接口失败时页面保留原状态并显示稳定错误码，不把失败操作显示为成功。
- Collector 操作不会触发 A6 渠道、Listing、Pin、余额划转或 Enodot 调度变更。

## P0-3：旧名称 Seller 达标后退休为纯 Provider

M1、M2、M3、M6、M9、M10、M11、M12、M13、M15、M17、M18 不立即删除或替换。
在可提现未达到 Session 的 `quota_per_unit` 前，它们继续保留 Seller Listing 并参与动态定价；
当前 `quota_per_unit=500000`，即最低转入阈值为 `$1`。

### 自动退休顺序

1. Collector 快照达到阈值后，重新读取该账号的实时用户余额和实时可提现。
2. 对实时全部可提现执行 `balance_transfer_preview`，先落幂等 Intent，再执行一次转入用户余额。
3. 回读验证用户余额增加或可提现按本次金额减少；结果不确定时只进入
   `transfer_unknown` 等待回读，不自动重复提交。
4. 保持 Seller 身份的同时临时加入 Provider duty，使用现有 A6 Token 幂等创建 Enodot
   Provider 绑定；并发固定为 `100`，初始 `schedulable=false`。
5. 下架该账号全部在线 Listing 并逐项回读，确认不存在可售 Listing。
6. 将角色切为单独的 `provider`，永久退出 Seller 动态定价目录。
7. Pin 控制器按普通 Provider 流程选择非自有渠道、探活并恢复 Enodot 调度。

### 并发与恢复约束

- 每轮最多退休一个商户，Seller Pilot 与退休器使用同一独占锁，仅串行化实际 Seller 写入，
  不暂停全局定时收益链路。
- Seller Pilot 每轮从数据库动态选择仍含 `seller` 角色的账号；纯 Provider 不会被重新上架。
- M1、M2、M3 当前承担 Observer。退休任一 Observer 后必须仍保留至少 2 个 Observer；新
  Observer 未就绪时，其余 Observer 即使达到 `$1` 也延后退休。
- 每一步写退休状态和事件审计；失败保留在当前可恢复步骤，5 分钟后重试。
- `boot`、`Sam Altman`、`acme`、`MingZi` 不在退休目标清单中，继续作为正常 Seller 使用。
