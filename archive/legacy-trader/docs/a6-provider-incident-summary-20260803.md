# A6 Provider 事故与恢复状态总结（2026-08-03）

## 结论

当前存在两个彼此独立的问题：

1. `ai.enodot.shop` 主站曾因本机 Sub2API `48768` unit 被人工停止而持续返回 `502`。该问题已于
   2026-08-03 02:08（Asia/Shanghai）恢复，公网首页现为 `200`。
2. Provider 自动恢复受 A6 Panel 控制面故障阻断。A6 API Key 推理 / Probe 与 A6 Panel 是两条链路：
   API Key 请求保持直连，A6 Panel 登录、Marketplace 和 Pin 才使用账号代理。当前故障发生在后者。

在 A6 Panel 恢复稳定响应以前，继续启动 Observer、Pin 或 Provider Apply 只会重复写入 transport/session
失败并增加账号风控风险。因此近期暂停这三条链路，保留主站和只读管理面板。

## `ai.enodot.shop` 停机

- 公网 DNS、TLS 和 Caddy 均正常。
- Caddy 持续返回 `502`，日志为：
  `dial tcp 127.0.0.1:48768: connect: connection refused`。
- `sub2api-callback-candidate-48768.service` 在 2026-08-02 15:25:57 UTC 收到停止操作，随后正常 cleanup，
  不是程序崩溃。
- Caddy 配置仍将主站、管理 API 和请求 API 转发到 `127.0.0.1:48768`，因此 unit 停止后整个主站失效。
- 2026-08-03 02:08（Asia/Shanghai）重新启动该 unit；当前：
  - `https://ai.enodot.shop/`：`200`
  - `https://ai.enodot.shop/a6-trader/`：未携带登录信息时为预期 `401`
  - `48768`、`48767`、A6 Trader dashboard `8765`：均在运行

## A6 外部故障证据

Observer 使用 12 个账号，每个账号通过固定 Panel 代理访问 `https://a6api.com`。最近所有 Observer 轮次均未
达到 quorum，主要错误为：

- `pin_a6_transport_failed`
- `a6_transport_failed`
- `pin_session_relogin_failed`

2026-08-03 01:55-02:00（Asia/Shanghai）的检查结果：

- 12 个 Observer 代理端口中，8 个在 TLS 建连后 8 秒内没有 HTTP 响应，1 个 TLS 超时，其余返回
  Cloudflare `503/521`。
- 抽样完整响应为 HTTP `521`，`Server: cloudflare`，正文 `error code: 521`。
- 对本机全部 68 个代理出口并发检查，没有一个取得正常 A6 Panel 响应。
- 本机、`us_cloud`、`hk_cloud`、`win_gw_lou` 的匿名只读请求同样超时或返回 `503/521`。

这说明当前不是某一个 Provider、Channel、槽位或 API Key 的问题，而是 A6 Panel / Marketplace 控制面当前
整体异常或极不稳定。

## API Key 与代理边界

以下边界作为硬约束：

- API Key inference / activation Probe：直连 `https://a6.a6api.com`，`proxy_url=""`。
- A6 Panel 登录、Pin、Marketplace：使用对应账号的固定代理。
- `api.a6api.com`、`a6.a6api.com` 与 `a6api.com` 的响应不得混算。

生产 v141 代码已在两个位置拒绝代理 Probe：

- 构造 Probe client 时固定 `A6ApiClient(_DIRECT_A6_ORIGIN, proxy_url="")`。
- Provider remote adapter 检测到非固定 origin 或非空 proxy 时立即报
  `provider_remote_probe_client_not_direct`。

## Provider 当前真实状态

截至 2026-08-03 02:08（Asia/Shanghai）：

- 数据库 active routing / cost generation：`7074 / 7074`
- 数据库 active slots：`2 READY`，Channel `815 / 1785`
- inventory：`0 READY`、`4 WARM_STANDBY`、`6 REPAIRING`、`7 RETIRED`
- 实际 schedulable：`0`
- assignment：`0`
- 最近失败候选 generation：`7200`，目标为 4 槽，Channel `1322 / 1813 / 706 / 1242`
- Publication 数据仍显示 `published / published`，但 generation 未收敛。

因此面板里的 `7074` 不是新一轮成功，而是数据库保留的最后成功父代。r7200 在激活前对远端运行态做过摘流，
随后 A6 Panel transport/session 失败；旧状态机只把 r7200 标记为 failed，没有完整恢复父代的 Pin、成本、
priority、metadata、billing floor 和 schedulable。结果是数据库仍有 2 个 READY，实际 inventory 全部
WARM_STANDBY，用户请求可调度数为 0。

## 已上线内容

生产当前 release：

`/www/a6-trader/releases/20260802T174547Z-v141-provider-parent-compensation`

v141 已增加：

- durable platform backoff 写前检查；backoff 期间跳过 generation 创建、摘流、改价和 repin。
- 激活前失败时执行父代补偿，恢复父代成本、Pin、priority、metadata、billing floor 和 schedulable，并做
  strict readback。
- 父代补偿后清空候选 Probe 复用标记。
- A6 Panel 幂等 GET 增加一次有界 transport retry；Pin POST 保持单次。
- API Key Probe 保持固定直连。

v141 回归结果：定向 `211 passed`，Provider 扩大回归 `481 passed`，Ruff 通过。

这些改动保护后续 generation 失败时的父代恢复；当前已经发生的远端状态漂移仍需要 A6 Panel 恢复后做一次
正式补偿或新的自动 Apply 才能收敛。

## 尚未上线内容

本地准备了两项 Observer 修正，当前未部署：

- PostgreSQL durable round cursor：失败轮也推进下一组，避免 23 秒失败周期反复命中同一组账号。
- 两次幂等 GET 共用一个总超时，并把 Observer client budget 调整为 3 秒，使 0/2/4/6 秒错峰的一轮在约
  9 秒内收口。

相关定向验证为 `71 passed + 14 passed`，Ruff 通过。因 A6 Panel 当前整体故障，本次停止构建和发布 v142，
避免在外部故障期间继续扩大变更面。

## 当前开关

保持运行：

- `sub2api-callback-candidate-48768.service`
- `sub2api-bluegreen-48767.service`
- `a6-trader-dashboard.service`

保持停止：

- Observer timer
- Pin timer
- Provider Pool Apply timer
- Provider Shadow timer
- Session Refresh timer
- Provider Dispatch / Failure Repair / Publication / Seller 下游 timer

## 恢复门槛

后续仅在以下条件全部满足后继续 Provider 恢复：

1. A6 Panel 经至少两个不同账号代理连续取得正常 JSON 响应，不再出现 `503/521` 或 TLS 后无响应。
2. 至少两个 Observer 生成新鲜、完整、满足 quorum 的快照。
3. 正式 Apply 由系统自动选 Pin，不做人工换 Pin，不运行 Shadow。
4. 验收至少 `4 READY`、`VACANT=0`、实际 schedulable 至少 4，且 Pin / Probe / 成本 / priority /
   metadata / billing floor readback 一致。
5. active routing/cost generation 推进且 Publication 收敛后，再依次恢复下游 timer。
