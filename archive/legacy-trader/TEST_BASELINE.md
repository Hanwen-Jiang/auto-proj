# Legacy Test Baseline

冻结快照应从本目录运行，旧测试中有大量相对 `migrations/`、`deploy/` 和 `scripts/` 的路径合同：

```bash
cd archive/legacy-trader
PYTHONPATH="src:." ../../.venv/bin/python -m pytest
```

依赖 treasury inventory 的三个数据测试还需要在本地临时提供：

```text
src/a6api_auto/data/usdt_wallet_inventory.json
```

该运行数据保存在仓库根 `.local/legacy-trader/runtime-data/`，不进入 Git。

## 2026-08-11 基线

```text
collected 2209 items
2200 passed
8 failed
1 skipped
duration 69.84s
```

已知失败来自整理前工作树自身，不由目录归档产生：

| 类别 | 数量 | 现象 |
| --- | ---: | --- |
| Observer/systemd 参数断言 | 3 | 测试仍期待旧 timeout、window 和 client-timeout，unit 已使用较新参数 |
| Seller systemd/gate 参数断言 | 2 | 测试与 unit 的 maximum multiplier、SuccessExitStatus 口径不同 |
| Authenticated client fixture | 1 | 新 profit-setting 读取触发了旧 `fixture-dsn` 测试夹具之外的连接 |
| Cashout PostgreSQL fixture | 2 | 候选读取返回空集，与旧 fixture 的 freshness/reserve 预期不同 |

对应失败测试：

```text
test_shadow_reconcile_a6_provider_pool.py::test_observer_round_scans_every_configured_observer_without_a_fixed_cap
test_trade_authenticated_client.py::test_seller_price_band_cli_factory_uses_cached_refreshed_session
test_trade_cashout_postgres_integration.py::test_candidate_loader_enforces_freshness_and_merchant_route_binding
test_trade_cashout_postgres_integration.py::test_provider_drain_resumes_the_same_intent_and_fund_reservation
test_trade_managed_autoonboard.py::test_systemd_timer_queues_durable_onboarding_under_shared_seller_lock
test_trade_pin_runner.py::test_observer_systemd_runs_complete_pool_for_event_driven_auto_pool
test_trade_pin_runner.py::test_observer_systemd_uses_bounded_get_budget_and_durable_cursor_version
test_trade_seller_control_gates.py::test_systemd_services_validate_gates_without_conditioning_timers
```

归档保持这些差异，不用修旧测试的方式改变冻结业务快照。新模块的验收测试按根目录 Prompt 重新建立。
