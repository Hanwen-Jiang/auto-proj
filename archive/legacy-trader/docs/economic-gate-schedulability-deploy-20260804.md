# Economic gate + listing schedulability deployment — 2026-08-04

协调备忘（给同时工作的 auto-pool agent / release agent）。

## 已部署到 us_cloud（current release，hot-patch + drop-in）

1. **k_CNY 请求面利润门禁已启用**（`/etc/a6-trader.env`）：
   - `ECONOMIC_PLAN_ONLY=false`、`ECONOMIC_PROJECTION_APPLY_ENABLED=true`、`ECONOMIC_PRIORITY1_ENABLED=true`
   - 门禁 = `source_k_cny × (1 + 6000bps) × 1.2`（A6 扣 20% 利润 → 有效 9200bps，45931 门禁 = ¥0.235446/K）
   - `ECONOMIC_CHANNEL_REVENUE_RECONCILE_ENABLED=true`、`ECONOMIC_FX_CNY_PER_USD=7.2`、`ECONOMIC_REVENUE_WINDOW_SECONDS=900`（revenue snapshot 已开始从 usage_logs 写入，83 channels）
   - economic-priority1 单元加了 drop-in：`/etc/systemd/system/a6-trader-economic-priority1.service.d/10-redis.conf`
     （`Environment=PROVIDER_DISPATCH_REDIS_URL=redis://127.0.0.1:6379/0`——仓库里的单元文件已同步该行）

2. **新常驻 duty**：`a6-trader-listing-schedulability.timer/service`（30s 周期）
   - 无任何可调用 sub2api-account 且无 schedulable provider → 全部 listing 下架
   - 有可调用 account → 仅 k_CNY ≥ 门禁的 listing 上架；merchant 自身 provider 可调度 → 上架
   - 全部 keep 时零网络调用，秒级完成，不占锁

3. **publication reconciler 调度门禁**：`trade_model_publication.py` 的 `seller_cost_guard_plan` 增加
   `schedulable_sources` 检查——无可调用源时禁止 resubmit（防止重挂竞赛）。

## 影响与注意事项

- **所有 gpt-5.6-sol listing 当前全部下架**（0 live），14 个经济源站全部 EXHAUSTED。
  当任一 account 恢复 AVAILABLE（或 provider 可调度）时：duty 会按门禁自动上架；
  请求面按 eligibility 路由，低于门禁的 channel 不会被路由到经济源站。
- 改动的代码文件（均在本地工作树，未提交前请勿基于旧快照出 release）：
  `src/a6api_auto/trade_economic_facts.py`、`trade_economic_source.py`、`trade_model_publication.py`、
  `scripts/reconcile_a6_economic_sources.py`、`scripts/reconcile_a6_listing_schedulability.py`、
  `deploy/systemd/a6-trader-economic-priority1.service`、`a6-trader-listing-schedulability.{service,timer}`、
  `deploy/a6-trader.env.example` + 对应测试。
- 若你要出 release：**从当前工作树构建**（包含以上文件）；若从 git 快照构建，untracked 文件会丢——
  请先确认经济子系统文件已提交（本会话已提交，见 git log）。
- 回滚：`current` 切回 v195 前的 release + env 还原（备份在 `/var/lib/a6-trader/deploy-backups/20260804T133742Z`）+ 停
  `a6-trader-listing-schedulability.timer` + 删 drop-in。
