# 生产请求面恢复记录（2026-08-01）

## 结论

- 恢复时间：2026-08-01 15:51:05 UTC（23:51:05 CST）。
- 权威请求面：`us_cloud` 上的 `sub2api-callback-candidate-48768.service`。
- 正式域名已全部从故障候选 `127.0.0.1:48769` 回切到 `127.0.0.1:48768`。
- 2026-08-01 16:02 UTC 的十分钟观察窗内，真实请求 `56/56` 返回 HTTP 200；账号选择失败、上游失败和服务 ERROR 均为 0。
- `hk_cloud` 保持权重 0 的影子节点；`win_gw_lou` 保持未接入状态。

## 根因证据

1. Enodot 远端账号曾全部为 `schedulable=false`，但 Trader active pool 仍把账号 `45895`、channel `1170` 投影为 `READY` 且可调度。
2. Provider inventory 同时显示 `ready_count=0`、`schedulable_count=0`，与 active pool 冲突。
3. 正式域名当时全部指向 v161/`48769`。启用真实可用账号后，v161 仍在请求前返回 `a6 provider replay attribution is missing or inconsistent`。
4. 同一 seller key 的端口探测结果：`48767=200`、`48768=200`、`48769=503`。

## 已执行动作

- 只恢复经真实请求验证且余额充足的账号：
  - `45917`: `active`, `schedulable=true`
  - `45913`: `active`, `schedulable=true`
- Caddy 四处正式请求 upstream 从 `48769` 回切到 `48768`，配置校验和热重载成功。
- Caddy 变更前备份：`/etc/caddy/Caddyfile.before-recovery-20260801T155105Z`。
- 当前 Caddyfile SHA256：`fe247f0b6bc3315d0adc31f7a41a63d7b54d5dd10e6ce694e6187da37b839caa`。
- 故障候选 `sub2api-paid-send-candidate-48769.service` 已设为 `disabled/inactive`。
- 以下会写 Provider 状态的入口保持 `disabled/inactive`：
  - `a6-trader-pin.timer`、`a6-trader-pin.path`
  - `a6-trader-provider-pool-apply.timer`
  - `a6-trader-provider-dispatch.timer`
  - `a6-trader-provider-failure-repair.timer`
  - `a6-trader-model-publication.timer`、`a6-trader-model-publication.path`

## 验证

- 十分钟请求窗：56 个请求全部 HTTP 200，平均延迟约 4899 ms。
- 账号分布：`45913=3`、`45917=53`。
- `account_select_failed=0`，`upstream_failures=0`，服务 ERROR 行数为 0。
- 公共域名已验证 Chat 非流式、Chat SSE、Responses SSE；均完成并返回 HTTP 200。
- `us_cloud`: Caddy、`48768`、Dashboard `8765`、Relay `8767` 正常。
- `hk_cloud`: shadow、state tunnel、relay 正常；`48770` 仅监听 loopback，readiness 因 `feature_flags_disabled` 返回 503，符合未发布状态。
- `win_gw_lou`: SSH 正常，未发现 `48767-48770`、`8765`、`8767` 监听。
- Collector 16:00 UTC 一轮采集 `49/51` 成功；`6375`、`9170` 各有一次 `a6_transport_failed`，两者上一轮仍成功且 freshness 为 fresh，不影响正式请求面。

## 隔离项

- Provider pool 仍错误指向 generation `7069`、账号 `45895`、channel `1170`，而 Provider inventory 没有 READY 或 schedulable 项。
- 远端实际承载流量的是 `45917`、`45913`；该事实尚未收敛到 Trader pool/inventory 投影。
- v161/`48769` 的 replay candidate snapshot 与 active pool attribution 冲突，修复并从干净基线复现前保持离线。
- 修复 `trade_provider_inventory.py` 与 provider pool reconciler 的投影收敛后，再逐项恢复自动写入 timer；每次只启用一个入口并完成读回验证。
