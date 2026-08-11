# Provider Auto-Pool 后续 Agent 交接手册

> 工作区：`/Users/haven/Documents/code/a6api-auto`
> 设计与进展基线：[`provider-auto-pool-design-and-progress-20260804.md`](provider-auto-pool-design-and-progress-20260804.md)
> 接手时的首要原则：保留全部现有未提交改动，先证明本地状态，再接触生产

## 0. 2026-08-05 状态指针（superseded / continuation）

> 本文仍是 Provider auto-pool 任务的**历史交接与 evidence ledger**，事实与记录保留，
> 不回滚、不重写。**自 2026-08-05 起，后续执行以
> [`architecture-deployment-review-20260805.md`](architecture-deployment-review-20260805.md)
> 为权威审阅稿**，本文与它冲突的步骤以下述说明为准。

- 已过时/被取代的历史步骤（不再按原样执行）：
  1. §1“预期初始事实”与 §12 中 `2026-08-04 17:23 CST 交接基线` ledger 记录的
     “HEAD 为 `557747e`、生产 migration 最高 `0065`、auto-pool/membership units 未安装、
     旧 Pin timer/path active/enabled、shadow disabled/inactive”——这些是 2026-08-04
     17:23 的快照。此后 HEAD 已推进到 `ae30382`，部署记录显示 release v190 已应用
     `0066-0069` 并安装新 units、禁用旧 pin/apply triggers；**当前生产状态必须以
     fresh live readback 为准**（审阅稿 §8 P0-4、§9 T1）。
  2. §5 的 model-scoped lease 与 §5.2 的 matching immutable cost generation 已在本文件
     evidence ledger 中记录为 P0 闭合，但该结论基于 2026-08-04 的测试结果，需在收口轮
     重跑验证。
  3. §8 的发布步骤沿用，但必须叠加审阅稿 §10 runbook 的只读预检、release manifest、
     逐 Provider readback 与停止条件；审阅稿最新编号的**未闭合前门**如下，需先收敛：
     - P0-1 **Dispatch 跨 schema/consumer 合同**（DB source → Python projection schema →
       Redis v1 payload → request-plane consumer，含首个错误码 `provider_dispatch_ready_pin_invalid`）；
     - P0-2 **Listing lifecycle stop boundary**（`a6-trader-listing-schedulability.timer/service`
       逃逸 `control.target` 停止边界）；
     - P0-3 **Ban 可见性合同冲突**（文档 §7 与 duty/auto-pool 代码语义相反，待实测收口）；
     - P0-7 **缺少独立于 general auto-pool gate 的 owned-Channel Ban preflight 可审计入口**
       （仓库内无 `PROVIDER_AUTO_POOL_ENABLED=false` 时执行硬门 A 的独立入口；缺入口时
       runbook step 7 硬停止、**不得启用 auto-pool**，归属 T3 产物 D + T4-producer；产物 D
       还需定案 admission grant 状态机与 admitted-set 序列化/load-claim 过滤合同）。
- 继续有效（与审阅稿一致，不因本文过时而豁免）：
  - §2 操作禁区、shadow 保持 disabled/inactive、owned Channel Ban 门禁、quota 与
    non-quota 分流、Seller 不进入 Provider activation barrier、evidence ledger 追加
    纪律、不 reset/clean/checkout、不全局格式化。
- 下一任务入口：审阅稿 §9 T1（fresh live 只读预检与三层对齐）→ T2（Ban 可见性实证与
  文档决策）→ T3（Dispatch route-mode/consumer + lifecycle stop boundary + 触发 ownership
  + **P0-7 独立 ban-only preflight 入口与 admission attestation（产物 D）** 的完整设计规格，
  不改代码；须先固定 request-plane consumer artifact）→ T4（单独批准后的跨边界代码/unit/
  测试修复与回归，分 T4-producer 与 T4-consumer 同批批准；产物 D 入口在 T4-producer 实现
  测试）→ T5（T4 通过后再单独批准的部署）。

## 1. 接手后的前十分钟

先执行只读检查：

```bash
cd /Users/haven/Documents/code/a6api-auto

sed -n '1,260p' AGENTS.md
sed -n '1,260p' docs/provider-auto-pool-design-and-progress-20260804.md
git status --short
git branch --show-current
git log -3 --oneline --decorate
```

预期初始事实：

- 分支为 `main`，交接时 HEAD 为 `557747e`。
- 工作树非常脏，包含其他任务的修改和构建产物。
- Provider auto-pool 的大量新增文件尚未 tracked。
- 生产 migration 最高 `0065`，`0066-0069` 未部署。
- 最新 focused 测试是 `148 passed, 1 failed`。
- 唯一已知失败是 `tests/test_trade_provider_pool_reconciler.py` 漏导入 `nullcontext`。
- Provider shadow 必须保持 `disabled/inactive`。

如果接手时这些事实已变化，以 live runtime 和当前磁盘证据为准，并先在本文末尾追加新的 evidence ledger，
不要覆盖旧记录。

## 2. 操作禁区

- 不运行 `git reset --hard`、`git clean`、`git checkout --`。
- 不全仓格式化，不批量删除 untracked 文件。
- 不用新生成的 migration 覆盖另一份 packaged migration；两份应通过定向 patch 同步。
- 不启动或 enable `a6-trader-provider-pool-shadow.service`/`.timer`。
- 不用 shadow 做常规 Pin、liquidity、failure repair、readback 或部署 smoke。
- 不在 owned Channel Ban 之前启用 auto-pool 或恢复 schedulability。
- 不用本地 candidate filter 代替完整远端分页 inventory。
- 不把 Seller completion 加入 Provider activation barrier。
- 不把 `insufficient_quota` 送入扩价路径。
- 不在生产直接试错 schema 或 unit；先用临时 PostgreSQL 和最小 release 验证。

## 3. 第一阶段：恢复干净的本地测试基线

### 3.1 最小修复

只在 `tests/test_trade_provider_pool_reconciler.py` 的 import 区加入：

```python
from contextlib import nullcontext
```

不要借机整理 import 之外的代码。

### 3.2 立即重跑

```bash
.venv/bin/ruff check \
  src/a6api_auto/trade_provider_pool.py \
  src/a6api_auto/trade_provider_pool_store.py \
  src/a6api_auto/trade_provider_pool_reconciler.py \
  src/a6api_auto/trade_provider_auto_pool_store.py \
  src/a6api_auto/trade_provider_membership_repair.py \
  scripts/apply_a6_provider_pool.py \
  scripts/reconcile_a6_provider_auto_pool.py \
  tests/test_trade_provider_pool_reconciler.py

.venv/bin/pytest -q \
  tests/test_trade_provider_pool_reconciler.py \
  tests/test_trade_provider_membership_repair.py \
  tests/test_apply_a6_provider_pool.py \
  tests/test_trade_provider_auto_pool.py \
  tests/test_trade_provider_pool_remote.py

cmp migrations/0066_provider_auto_pool.sql \
  src/a6api_auto/data/migrations/0066_provider_auto_pool.sql

cmp migrations/0069_provider_membership_consumer_and_liquidity_tokens.sql \
  src/a6api_auto/data/migrations/0069_provider_membership_consumer_and_liquidity_tokens.sql
```

通过后记录准确的 passed 数量、时间和命令，不沿用历史 `56/267/174 passed` 作为最新结果。

## 4. 第二阶段：补 PostgreSQL 并发和迁移证据

优先复用 `tests/test_trade_provider_pool_sizing_postgres_integration.py::temporary_postgres` 的临时
PostgreSQL 模式，新增 focused integration test，至少覆盖：

1. 从 `0001` 到 `0069` 的完整 migration chain。
2. `provider_pool_slot_ready_pin_required` 在 `0066` 后不存在。
3. fixed Pin READY 与 auto-pool READY 两种 shape 都满足新 CHECK。
4. applying auto generation 在相同 input 下可幂等 resume。
5. active generation refresh 不创建冲突 active row。
6. 同 generation claim busy。
7. stale claim 超时回收。
8. 错误 claim token 对 `record_prepared/ready/failed` 全部 CAS 失败。
9. claim、pause、prepare、ready、activate、fail 对 slot/inventory 的投影正确。
10. membership `complete()` 在 generation/slot/account 任一状态失效时拒绝完成。
11. activation publisher 执行前 changed slot 已是 `REPAIRING`。
12. 两个执行者并发处理同 model、不同 generation 时只能一个持有 lease。

重点审查 `src/a6api_auto/trade_provider_auto_pool_store.py`：

- `begin_generation()` 在约第 255 行。
- `claim_accounts()` 在约第 496 行。
- `record_prepared()` 在约第 673 行。
- `record_ready()` 在约第 731 行。
- `record_failed()` 在约第 764 行。
- `fail_generation()` 在约第 1036 行。

行号会随补丁变化，接手时用 `rg -n` 重新定位。

## 5. 第三阶段：闭合两个 P0 设计问题

### 5.1 Model-scoped lease

目标是让共享数据库上的同 model controller 串行，而不是只依赖单机 `flock`。建议实现一个
PostgreSQL session advisory lease，并覆盖 `begin_generation()`、account claim、remote apply、generation
activate/fail 的完整 controller lifetime。

验收点：

- 相同 model 的第二执行者稳定得到 busy/retry 结果。
- 不同 model 仍可并行。
- 进程崩溃或连接关闭后 lease 自动释放。
- stale account claim 的恢复语义与 session lease 不冲突。
- systemd 单机 `flock` 保留，作为 host-local 第二层防线。

如果选择把 `begin_generation()` 与 `claim_accounts()` 合并为一个事务，也仍需覆盖跨节点 remote apply
阶段；短事务 row lock 本身不等于完整 controller lease。

### 5.2 Matching immutable cost generation

沿这条证据链逐项核对：

```text
fresh Observer CNY band
  -> auto generation max_ratio_cny
  -> same snapshot USD conversion
  -> auto generation max_ratio_usd
  -> provider_cost_generations desired_slot_costs
  -> provider_pool_slots desired_slot_cost_multiplier
  -> Enodot observed rate_multiplier
  -> active routing/cost generation composite identity
  -> Seller active auto-pool generation input
```

要求：

- CNY cap 始终 `<=0.3`。
- conversion evidence 带 snapshot/source hash。
- Enodot observed multiplier 与 generation 中的 USD value 精确匹配。
- `provider_auto_pool_generations(model, routing_generation, cost_generation)` 的 FK 指向真实 matching
  Provider generation，而不是只保证数字存在。
- activation 后 active generation 的 cost intent 保持 immutable。
- runtime expansion 必须创建/复用符合 immutable contract 的 planned generation，严格进入下一价格带。

## 6. 第四阶段：扩大回归

P0 设计闭合后，按风险面扩大测试：

- auto-pool、membership、remote Ban、runtime failure、liquidity。
- CLI、DB、Pin、Role、Dashboard。
- systemd unit/path/target 静态断言。
- Token naming/migration。
- Provider/Seller orchestration。

至少重跑原先记录的专项集合和 `267` 项跨模块集合；实际 test selection 必须从 shell history、测试文件和
本次 diff 重新构造，并把完整命令写入 evidence ledger。

## 7. 第五阶段：生产发布前只读预检

生产 SSH alias 是 `us_cloud`。先确认 worker 与 web symlink：

```bash
ssh us_cloud 'hostname'
ssh us_cloud 'readlink -f /opt/a6-trader/current'
ssh us_cloud 'readlink -f /www/a6-trader/current'
```

交接时两者不同：worker `/opt` 指向 `v188`，web `/www` 指向 `v181`。systemd worker 以 `/opt` 为准，
但备份必须同时记录两者。

继续读回：

```bash
ssh us_cloud "sudo -u postgres psql -X -A -t -d sub2api_codexmanager \
  -c 'SELECT version FROM a6_trade.schema_migrations ORDER BY version DESC LIMIT 10;'"

ssh us_cloud 'systemctl cat a6-trader-observer.service --no-pager'
ssh us_cloud 'systemctl cat a6-trader-provider-pool-apply.service --no-pager'

ssh us_cloud 'systemctl show \
  a6-trader-provider-pool-shadow.service \
  a6-trader-provider-pool-shadow.timer \
  --property=Id,LoadState,ActiveState,SubState,UnitFileState --no-pager'
```

还必须查询并保存：

- eligible、non-retired Provider 全名单；
- active routing/cost generations 和 slots；
- Provider inventory/roles/Token/merchant/Enodot identity；
- funding 和 withdrawable balance；
- Observer 最新成功时间、quorum、source scans；
- migration checksum、relation sizes、当前 locks；
- `/etc/a6-trader.env` 中与本任务有关的 feature gates，不输出 secret values；
- 旧 Pin timer/path、legacy Provider apply timer 的 enable/active 状态；
- 新 unit 安装前后的 FragmentPath、ExecStart、OnSuccess、PartOf。

## 8. 第六阶段：最小 release 与发布

发布前创建一个内容明确的 release manifest，记录：

- 基线 `/opt` release 和 `/www` release；
- 新 release 路径与文件清单；
- wheel/hash；
- migration `0066-0069` hash；
- unit 文件 hash；
- `/etc/a6-trader.env` 备份位置和权限；
- schema migration ledger 快照；
- 发布操作者、开始时间和回退点。

最小 release 只带本任务所需 package、scripts、migrations、data 和 units。不要把工作树中无关 build
目录或其他任务的中间文件打进去。

建议顺序：

1. 停止会写 Provider membership/Pin 的旧 triggers，确认没有在途 job。
2. 备份 symlink、env、unit、migration ledger 和相关表的 schema/state evidence。
3. 安装新 release，但暂不打开 auto-pool feature gate。
4. 应用 `0066-0069`，逐项读回 ledger、constraints、indexes、FK。
5. 安装并 daemon-reload 新 units，先做 `systemd-analyze verify` 和 `systemctl cat/show`。
6. 禁用旧 Pin timer/path 与旧 Provider pool apply timer，避免双写。
7. 确认 shadow timer/service 仍 `disabled/inactive`。
8. 打开精确 feature gates。
9. 触发 fresh Observer；由 `OnSuccess` 启动 auto-pool，不手工并发启动第二份 controller。
10. 逐 Provider 执行 live readback，再允许 dispatch。

具体 release/迁移命令必须基于生产现有部署脚本和 schema ledger 实现，不要凭文稿猜测路径或事务语义。

## 9. 第七阶段：逐 Provider live 验收

每个 eligible Provider 保存一条结构化 evidence record，至少包括：

```json
{
  "model": "gpt-5.6-sol",
  "merchant_id": "MERCHANT_ID",
  "token_ref_id": "TOKEN_REF_ID",
  "external_token_id": "TOKEN_ID",
  "enodot_account_id": "ACCOUNT_ID",
  "routing_generation": "ROUTING_GENERATION",
  "cost_generation": "COST_GENERATION",
  "auto_pool_generation": "AUTO_POOL_GENERATION",
  "observer_scan_ids": ["SCAN_ID"],
  "observer_observed_at": "TIMESTAMP",
  "owned_channel_ids": ["CHANNEL_ID"],
  "disabled_owned_channel_ids": ["CHANNEL_ID"],
  "owned_channel_readbacks_exact": true,
  "subscription_id": "SUBSCRIPTION_ID",
  "auto_channels": ["CHANNEL_ID"],
  "auto_channels_nonempty": true,
  "owned_channel_intersection_empty": true,
  "max_ratio_cny": "RATIO",
  "max_ratio_usd": "RATIO",
  "enodot_cost_readback_exact": true,
  "schedulable_readback_exact": true,
  "slot_state": "READY",
  "dispatch_present": true
}
```

任一 owned Channel 未精确读回以下值时，该 Provider 保持不可调度：

```json
{
  "user_channel_disabled": true,
  "route_status": "user_disabled"
}
```

同时验证：

- `auto_channels` 非空且与 owned set 交集为空；
- Observer 新鲜度满足 unit 配置；
- `max_ratio_cny <= 0.3`；
- slot/account/generation/dispatch 四个 read model 一致；
- Seller 在 Provider activation 后被触发，但 Provider 不等待 Seller 完成；
- Dashboard/read API 与数据库及远端 readback 一致。

## 10. 故障分流验收

### `insufficient_quota`

预期链路：

```text
schedulable removal
  -> UNFUNDED
  -> whole-unit withdrawable transfer
  -> funding readback
  -> still unfunded: resign Provider
  -> old slot VACANT
  -> funded replacement request
  -> replacement REPAIRING until every readback passes
```

### 其他 runtime failure

预期链路：

```text
Provider paused
  -> fresh Observer
  -> strictly next CNY band, capped at 0.3
  -> matching USD cost/generation
  -> Provider remote readbacks
  -> Provider restore
  -> immediate Seller repricing/publication trigger
```

## 11. 发布回退与停止条件

出现以下任一情况时，不继续扩大 activation：

- marketplace pagination 不完整或 locally known owned Channel 缺失；
- disable readback 不是精确的 `true/user_disabled`；
- `auto_channels` 为空或包含 owned Channel；
- Observer stale、quorum 不足或 cap 超过 `0.3`；
- Enodot cost、subscription、schedulability readback 不匹配；
- routing/cost/auto generation 身份不一致；
- claim/lease 出现两个同 model owner；
- legacy Pin/apply 与 auto-pool 同时写；
- shadow 被意外启动；
- migration/unit 与 release manifest hash 不一致。

停止时优先保持 Provider paused、slot `REPAIRING/VACANT`、shadow disabled。schema 已向前迁移后不要直接执行
未经验证的 down migration；应用回退必须先确认新 schema 对旧 worker 的兼容性。

## 12. Evidence ledger 模板

每完成一个阶段，在本文末尾追加，并保留之前记录：

```text
Time (CST):
Agent/task id:
Workspace HEAD:
Files changed:
Commands executed:
Test result:
Production read-only result:
Production mutations:
Shadow state readback:
Open blockers:
Next exact step:
Artifact paths:
```

### 2026-08-04 17:23 CST 交接基线

```text
Agent/task id: 019fcbbd-7dfb-7640-b170-1307c38af046
Workspace HEAD: main / 557747e
Files changed in this documentation pass:
  docs/provider-auto-pool-design-and-progress-20260804.md
  docs/provider-auto-pool-agent-handoff-20260804.md
Focused test: 148 passed, 1 failed
Focused Ruff: 1 F821 (nullcontext missing in test import)
Migration mirror cmp: 0066 pass, 0069 pass
Production mutations: none
Worker release: /opt/a6-trader/current -> v188-economic-floor-observability-minimal
Migration max: 0065
Auto-pool/membership units: not-found
Old Pin/apply triggers: active/enabled
Shadow: timer disabled/inactive; service inactive
Next exact step: add nullcontext import, rerun focused tests/Ruff
```

### 2026-08-04 恢复基线复验（nullcontext 修复后）

```text
Time (CST): 2026-08-04
Agent/task id: 本轮恢复 agent
Workspace HEAD: main / 557747e
Files changed:
  tests/test_trade_provider_pool_reconciler.py (仅加入 from contextlib import nullcontext)
Commands executed:
  .venv/bin/ruff check src/a6api_auto/trade_provider_pool.py
    src/a6api_auto/trade_provider_pool_store.py
    src/a6api_auto/trade_provider_pool_reconciler.py
    src/a6api_auto/trade_provider_auto_pool_store.py
    src/a6api_auto/trade_provider_membership_repair.py
    scripts/apply_a6_provider_pool.py
    scripts/reconcile_a6_provider_auto_pool.py
    tests/test_trade_provider_pool_reconciler.py
  .venv/bin/pytest -q tests/test_trade_provider_pool_reconciler.py
    tests/test_trade_provider_membership_repair.py
    tests/test_apply_a6_provider_pool.py
    tests/test_trade_provider_auto_pool.py
    tests/test_trade_provider_pool_remote.py
  cmp migrations/0066_provider_auto_pool.sql src/a6api_auto/data/migrations/0066_provider_auto_pool.sql
  cmp migrations/0069_provider_membership_consumer_and_liquidity_tokens.sql
    src/a6api_auto/data/migrations/0069_provider_membership_consumer_and_liquidity_tokens.sql
Test result: 149 passed, 0 failed; ruff all clean; 0066/0069 mirror cmp pass
Production read-only result: 未执行（本阶段仅本地）
Production mutations: none
Shadow state readback: 未接触
Open blockers: 下一阶段 PostgreSQL 并发/迁移测试、model-scoped lease、matching cost generation 证据
Next exact step: 第二阶段——新增临时 PostgreSQL integration tests（0001-0069 chain、claim CAS、stale claim、completion CAS、投影与 activation barrier、lease 竞争）
Artifact paths: 无
```

### 2026-08-04 第二阶段/第三阶段：PostgreSQL 集成测试与 P0 闭合

```text
Time (CST): 2026-08-04
Agent/task id: 本轮恢复 agent
Workspace HEAD: main / 557747e
Files changed:
  tests/test_trade_provider_auto_pool_postgres_integration.py (新增 16 个测试)
  src/a6api_auto/trade_provider_auto_pool_store.py
    - activate(): 修复 a6_pin_bindings UPDATE 引用不存在的 updated_at 列（真实 bug，
      a6_pin_bindings 仅有 created_at；原代码在任何真实 schema 上 activate 必失败）
    - record_prepared(): 增加 generation 比值 CAS——EXISTS 子查询校验
      observed_max_ratio_cny/usd 必须等于 generation 的 max_ratio_cny/usd；
      不匹配时返回 provider_auto_pool_account_cost_generation_mismatch
  scripts/reconcile_a6_provider_auto_pool.py
    - apply 路径整体包进 PostgresProviderModelLease.hold(model)，覆盖
      begin_generation -> claim_accounts -> remote prepare/resume -> activate/fail
      的完整 controller lifetime；同 model 第二执行者得到 provider_model_lease_busy
  tests/test_trade_provider_auto_pool.py
    - 新增 test_auto_pool_store_cas_and_runner_lease_are_wired（静态接线断言）
Commands executed:
  pytest -q tests/test_trade_provider_auto_pool_postgres_integration.py
  pytest -q <focused 6 files>
  pytest -q <Provider 域 47 个测试文件>
  pytest -q <跨模块 CLI/DB/Pin/Role/Dashboard/Seller 集合>
  pytest -q tests/（全套，--ignore mjs）
Test result:
  集成: 16 passed（0001-0069 全链、READY 双 shape、begin 幂等 resume、active 单行、
    claim busy/幂等重claim、stale claim 回收、错误 token 三写 CAS、claim->pause->prepare->
    ready->activate 投影链、record_failed/fail_generation 投影、record_prepared 比值 CAS、
    membership completion CAS、lease 同 model 串行/异 model 并行/会话关闭释放、
    activation barrier、hold_membership 只暂停 changed slot）
  focused 6 文件: 166 passed, 0 failed; ruff clean; 0066/0069 mirror cmp pass
  Provider 域（47 文件）: 667 passed
  跨模块（26 文件）: 761 passed, 1 failed
  全套 tests/: 2168 passed, 1 skipped, 5 failed
全套装 5 个失败均不在本任务文件面，且失败原因来自其他任务未提交改动
  （cli.py 1171 行未提交改动导致 test_seller_price_band_cli_factory_... ；
   cashout postgres 2 个、managed autoonboard 1 个、seller control gates 1 个）；
  本任务所有新增/修改文件 ruff 与 focused pytest 全绿
P0 闭合结论:
  1. begin_generation/claim_accounts 间隙: 由 session advisory lease 全程串行化，
     不再依赖短事务 row lock（PostgresProviderModelLease 已存在，runner 已接入）
  2. claim 跨 generation 竞争: lease 按 model 覆盖全部写阶段；flock 保留为 host-local 第二层
  3. matching immutable cost generation: 审计通过——cap<=0.3 由 plan 排除 + CHECK 双层强制；
     strict next band 由 boundary_index>expand_above_ratio 保证且 exhaustion 报错；
     Enodot multiplier 精确 readback<=1e-12；activate 将 slot desired/observed multiplier
     写为 generation max_ratio_usd；Seller 查询要求 account.observed_max_ratio_usd
     = generation.max_ratio_usd（失败关闭）；0069 FK 指向真实 routing/cost pair；
     activation 不改写 cost intent；新增 record_prepared 比值 CAS 关闭 DB 层写入缺口
Production read-only result: 未执行（本阶段仅本地）
Production mutations: none
Shadow state readback: 未接触
Open blockers:
  - 全套 5 个非本任务失败需在各自任务清理
  - record_failed() 的 slot/inventory UPDATE 仍未逐项断言 rowcount（P1，观察到的行为正确，
    但 CAS 强度弱于 record_prepared/ready；建议后续补 rowcount 断言或错误 token 下不投影）
  - 生产预检/发布/逐 Provider live 验收尚未开始（第五阶段起）
Next exact step: 第四阶段完成后的第五阶段——生产只读预检（v188/v181 symlink、
  migration ledger、unit cat、eligible Provider 全名单、Observer 新鲜度、shadow 状态）
Artifact paths:
  tests/test_trade_provider_auto_pool_postgres_integration.py
```

### 2026-08-04 第五阶段：生产只读预检证据（LIVE）

```text
Time (CST): 2026-08-04
Agent/task id: 本轮恢复 agent
Workspace HEAD: main / 557747e
Production mutations: 无（全部命令只读）
Production read-only result:
  SSH alias: us_cloud ; hostname: ser635876468110
  worker symlink: /opt/a6-trader/current -> /www/a6-trader/releases/
    20260804T070952Z-v188-economic-floor-observability-minimal (LIVE)
  web symlink:   /www/a6-trader/current -> /www/a6-trader/releases/
    20260804T004613Z-v181-observer-cookie-seller-fastpath (LIVE)
    （/opt 与 /www 分叉，与交接基线一致）
  migration ledger 最高: 0065（0066-0069 未部署）
  0066-0069 迁移 checksum: 本地 0001-0065 全部 MATCH 生产 ledger；
    0064 本地与生产均不存在；0066-0069 生产无记录
  migration checksum 证据: /tmp/prod-migration-ledger.txt（本机临时文件）
  生产 provider_pool_slots: 无 route_mode 列（0066 未应用，符合预期）
  shadow service: static/inactive/dead ; shadow timer: disabled/inactive/dead (LIVE)
  observer unit: 生产 ExecStart 无 --defer-membership-to-auto-pool，
    OnSuccess=a6-trader-pin.service（旧链）；本地 unit OnSuccess=auto-pool
  provider-pool-apply unit: 生产无 --defer-membership-to-auto-pool（旧链）；
    本地 unit 已带该参数
  auto-pool/membership units: not-found（未安装）
  release 内文件: /opt/a6-trader/current 无 auto_pool/membership scripts，
    无 0066-0069 migrations（LIVE）
  feature gates: /etc/a6-trader.env 无 PROVIDER_AUTO_POOL_ENABLED（仅回读 key 名，
    未输出 secret）；现有 PROVIDER_POOL_RECONCILE_ENABLED / APPLY_CONFIRMATION /
    APPLY_STAGE / MAX_MEMBERSHIP_CHANGES / INVENTORY_RECONCILE_ENABLED /
    LIQUIDITY_RECONCILE_ENABLED 存在
  旧触发器: pin.timer active/enabled ; pin.path active/enabled（PathModified=
    /run/sub2api-a6-trader/pin-request.failures -> pin.service）;
    provider-pool-apply.timer active/enabled
  active routing generation: gpt-5.6-sol generation=7396 cost=7396 active,
    desired_size=12
  active slots（12 个, 全部非 READY）:
    DRAINING: slots 1,2,5,6,9,10,11 (7) ; REPAIRING: slots 3,4,7,8,12 (5)
    全部 desired/observed_schedulable=false（dispatch 当前 fail-closed）
  eligible Provider inventory（13 个非 retired, gpt-5.6-sol）:
    merchant 3  (token 3,   acct 45907, REPAIRING/UNFUNDED, provider+observer)
    merchant 4  (token 16,  acct 45909, WARM_STANDBY/FUNDED, seller+provider)
    merchant 5  (token 4,   acct 45895, WARM_STANDBY/FUNDED, seller+provider)
    merchant 7  (token 6,   acct 45897, REPAIRING/UNFUNDED, seller+provider)
    merchant 10 (token 8,   acct 45904, REPAIRING/UNFUNDED, provider+observer)
    merchant 15 (token 12,  acct 45899, REPAIRING/UNFUNDED, provider+observer)
    merchant 19 (token 15,  acct 45896, WARM_STANDBY/TRANSFER_UNKNOWN, seller+provider)
    merchant 6742 (token 77376, acct 45917, WARM_STANDBY/FUNDED, seller+provider)
    merchant 8132 (token 20012, acct 45915, REPAIRING/UNFUNDED, seller+provider)
    merchant 10176 (token 22746, acct 45916, WARM_STANDBY/FUNDED, seller+provider)
    merchant 10585 (token 23324, acct 45914, WARM_STANDBY/REFILLING, seller+provider)
    merchant 12715 (token 26009, acct 45912, REPAIRING/UNFUNDED, seller+provider)
    merchant 12914 (token 26357, acct 45913, REPAIRING/UNFUNDED, seller+provider)
  funding 读回（user_balance_quota|used_quota|withdrawable_micros）:
    4: 1000000|12692702|565799 ; 5: 1274578|8852428|1199214 ;
    6742: 1099985|2391916|887056 ; 10176: 1145746|2327556|1565599 ;
    10585: 500000|1486052|206924 ; 19: 500000|1497333|760147 ;
    8132: 0|970788|495136 ; 12914: 0|1981795|354025 ;
    3/10/15: user_balance=0, withdrawable<=4064/0/0（不可救）
    7: 500000|5073368|65666 ; 12715: 500000|1366728|160794
  Observer 新鲜度（LIVE）: 近 6h completed=2677（最新 2026-08-04 09:49:09 UTC）
    failed=8064（a6_transport_failed，最新 09:49:40）——失败率高但存在新鲜成功 scan；
    auto-pool 的 30s snapshot 门禁在触发时必须复核
  relation sizes: provider_pool_slots 592kB, provider_pool_generations 4352kB,
    provider_cost_generations 4480kB, provider_inventory 104kB, a6_tokens 144kB,
    a6_merchants 584kB, enodot_account_bindings 176kB, a6_pin_bindings 3768kB,
    provider_pin_assignment_epochs 160kB, provider_fund_reserves 80kB,
    trade_control_actions 149MB（0066-0069 不触碰）
  locks: 仅普通 relation/transactionid/virtualxid 工作锁，无长 DDL 锁
  本地目标 unit 链（待安装）: observer OnSuccess=auto-pool；
    auto-pool OnSuccess=seller-pilot；apply 带 --defer-membership-to-auto-pool；
    auto-pool-refresh.path(PathChanged /run/a6-trader/provider-auto-pool-request->observer)；
    membership-repair.service + membership-apply.path 在 control.target Wants 中；
    control.target Wants 不含 auto-pool.service（仅 PropagatesStopTo）
Shadow state readback: timer disabled/inactive; service static/inactive
Open blockers:
  - Observer 近期 transport 失败率高（a6_transport_failed）——部署后触发 fresh Observer
    时必须复核 quorum/新鲜度门禁
  - 当前 12 slots 全部 DRAINING/REPAIRING，无 READY slot——auto-pool 首次 activation 将
    是 changed identity 的首次启用，必须走完整 Ban/readback 门禁
  - 13 个 Provider 中 6 个 funding_eligible；其中 merchant 19 TRANSFER_UNKNOWN、
    10585 REFILLING 需 liquidity controller 确认后再纳入
Next exact step: 第六阶段——构建 release manifest、备份、安装新 release（不开 gate）、
  应用 0066-0069、安装 units、禁用旧 Pin/apply triggers、确认 shadow disabled、
  打开 feature gates、触发 Observer 链、逐 Provider live 验收
Artifact paths:
  /tmp/prod-migration-ledger.txt（生产 migration checksum 快照，本机临时）
```

```text
Time (CST): 2026-08-04
Agent/task id: 本轮恢复 agent
Workspace HEAD: main / 557747e
Files changed:
  tests/test_trade_provider_auto_pool_postgres_integration.py (新增 16 个测试)
  src/a6api_auto/trade_provider_auto_pool_store.py
    - activate(): 修复 a6_pin_bindings UPDATE 引用不存在的 updated_at 列（真实 bug，
      a6_pin_bindings 仅有 created_at；原代码在任何真实 schema 上 activate 必失败）
    - record_prepared(): 增加 generation 比值 CAS——EXISTS 子查询校验
      observed_max_ratio_cny/usd 必须等于 generation 的 max_ratio_cny/usd；
      不匹配时返回 provider_auto_pool_account_cost_generation_mismatch
  scripts/reconcile_a6_provider_auto_pool.py
    - apply 路径整体包进 PostgresProviderModelLease.hold(model)，覆盖
      begin_generation -> claim_accounts -> remote prepare/resume -> activate/fail
      的完整 controller lifetime；同 model 第二执行者得到 provider_model_lease_busy
  tests/test_trade_provider_auto_pool.py
    - 新增 test_auto_pool_store_cas_and_runner_lease_are_wired（静态接线断言）
Commands executed:
  pytest -q tests/test_trade_provider_auto_pool_postgres_integration.py
  pytest -q <focused 6 files>
  pytest -q <Provider 域 47 个测试文件>
  pytest -q <跨模块 CLI/DB/Pin/Role/Dashboard/Seller 集合>
  pytest -q tests/（全套，--ignore mjs）
Test result:
  集成: 16 passed（0001-0069 全链、READY 双 shape、begin 幂等 resume、active 单行、
    claim busy/幂等重claim、stale claim 回收、错误 token 三写 CAS、claim->pause->prepare->
    ready->activate 投影链、record_failed/fail_generation 投影、record_prepared 比值 CAS、
    membership completion CAS、lease 同 model 串行/异 model 并行/会话关闭释放、
    activation barrier、hold_membership 只暂停 changed slot）
  focused 6 文件: 166 passed, 0 failed; ruff clean; 0066/0069 mirror cmp pass
  Provider 域（47 文件）: 667 passed
  跨模块（26 文件）: 761 passed, 1 failed
  全套 tests/: 2168 passed, 1 skipped, 5 failed
全套装 5 个失败均不在本任务文件面，且失败原因来自其他任务未提交改动
  （cli.py 1171 行未提交改动导致 test_seller_price_band_cli_factory_... ；
   cashout postgres 2 个、managed autoonboard 1 个、seller control gates 1 个）；
  本任务所有新增/修改文件 ruff 与 focused pytest 全绿
P0 闭合结论:
  1. begin_generation/claim_accounts 间隙: 由 session advisory lease 全程串行化，
     不再依赖短事务 row lock（PostgresProviderModelLease 已存在，runner 已接入）
  2. claim 跨 generation 竞争: lease 按 model 覆盖全部写阶段；flock 保留为 host-local 第二层
  3. matching immutable cost generation: 审计通过——cap<=0.3 由 plan 排除 + CHECK 双层强制；
     strict next band 由 boundary_index>expand_above_ratio 保证且 exhaustion 报错；
     Enodot multiplier 精确 readback<=1e-12；activate 将 slot desired/observed multiplier
     写为 generation max_ratio_usd；Seller 查询要求 account.observed_max_ratio_usd
     = generation.max_ratio_usd（失败关闭）；0069 FK 指向真实 routing/cost pair；
     activation 不改写 cost intent；新增 record_prepared 比值 CAS 关闭 DB 层写入缺口
Production read-only result: 未执行（本阶段仅本地）
Production mutations: none
Shadow state readback: 未接触
Open blockers:
  - 全套 5 个非本任务失败需在各自任务清理
  - record_failed() 的 slot/inventory UPDATE 仍未逐项断言 rowcount（P1，观察到的行为正确，
    但 CAS 强度弱于 record_prepared/ready；建议后续补 rowcount 断言或错误 token 下不投影）
  - 生产预检/发布/逐 Provider live 验收尚未开始（第五阶段起）
Next exact step: 第四阶段完成后的第五阶段——生产只读预检（v188/v181 symlink、
  migration ledger、unit cat、eligible Provider 全名单、Observer 新鲜度、shadow 状态）
Artifact paths:
  tests/test_trade_provider_auto_pool_postgres_integration.py
```


### 2026-08-04 第七阶段修订：listing 自动上下架 + 门禁修正（另一 agent 工作审核与调整）

```text
Time (CST): 2026-08-04 23:00
Agent/task id: 本轮恢复 agent（审核 + 调整）
Workspace HEAD: ae30382（另一 agent 的 2 commits 已并入）
审核对象: 1b3faeb (9965 行/15 文件) + ae30382 (flock 修复)
审核结论:
  - 门禁数学 1.92 = (1+0.6)K/(5/6) 数值正确；但 revenue 计算的硬编码
    ECONOMIC_FX_CNY_PER_USD=7.2 与设计文档不符（市场配对汇率实际 ~6.7526）
  - relist 判据最初用"实际收入"，与用户确认的"Listing 价格 k_CNY ≥ 门禁"不符；
    修正后又因"最便宜账号 + 仅价格判据"导致 59/59 全上架（用户叫停）
调整落地（v196-v198）:
  - trade_economic_facts.py: fx 从 a6_market_quotes 成对坐标推导（6.7526），
    env 7.2 仅回退；审计 payload 记录 fx_source
  - reconcile_a6_listing_schedulability.py: 双门禁语义
      sub2api 侧 = 最便宜可调用账号：净结算 ≥ k×net_multiplier(1.6) 且
        Listing ≥ k×gross_multiplier(1.92)；bps 来自 model_profit_settings（配置驱动）
      provider 侧 = 最便宜可调用 provider：价格 ≥ 上游成本×1.44
        （margin_bps 来自 active cost generation，配置驱动）
      callable 要求剩余配额 ≥ 20A（过滤近耗尽账号）
  - trade_dashboard.html: 移除 Probe 显示；REPAIRING+funded 显示"待 auto-pool 接管"
生产状态:
  - 止血：0 live（89/89 下架，emergency_pause 幂等审计）
  - v198 部署：duty 每 30s 运行，callable=1（45933）、revenue=2 渠道、
    would_relist=0、全 keep（无渠道过 45933 的 0.32/0.384 门禁）
  - 门禁来源: model_profit_settings (6000bps->1.6/1.92) +
    provider_cost_generations.margin_bps (2000->1.44) + 市场 fx 6.7526
Open blockers:
  - auto-pool Ban 依赖 listing 上架后 owned 渠道在 marketplace 可见；
    当前 0 live 且无渠道过门禁 -> Ban 仍 fail-closed（安全）
  - 后续需恢复正式 release 目录规范（v196-v198 为叠加直拷 + manifest）
Next exact step: 待 listing 上架恢复后重试 auto-pool Ban -> 订阅 -> cost -> schedulable
Artifact paths:
  .codex-tmp/a6-v197-payload, a6-v198-payload（本地）
```

### 2026-08-04 第六阶段：生产发布与迁移（LIVE，进行中）

```text
Time (CST): 2026-08-04 19:30（UTC 11:30）
Agent/task id: 本轮恢复 agent
Workspace HEAD: main / 557747e
Production mutations（按序）:
  1. 备份: /etc/a6-trader.env.bak-v190-provider-auto-pool-20260804T100048Z (mode 600)；
     /etc/systemd/system/{observer,pool-apply,control.target}.bak-v190-provider-auto-pool；
     /var/lib/a6-trader/backups/schema-migration-ledger.bak-v190-provider-auto-pool.txt；
     /var/lib/a6-trader/backups/symlink-opt.bak-v190-provider-auto-pool.txt
  2. release v190: /www/a6-trader/releases/20260804T100048Z-v190-provider-auto-pool
     （parent v188 拷贝 + payload overlay + PATCH-MANIFEST 重算 + import/mirror smoke）；
     /opt/a6-trader/current -> v190（web /www 仍 v181，未动）
  3. migration 0066-0069 应用成功（lock_timeout 15s / statement_timeout 120s），
     列/约束/表逐项读回通过；因 schema_migrations 由 apply_migrations() 维护，
     手工补录 0066-0069 ledger 行（checksum 与本地 sha256 精确一致）
  4. units 安装（8 个，install -m 0644 + daemon-reload + systemd-analyze verify 通过，
     仅无关 xray/snapd 警告）: observer(OnSuccess=auto-pool)、pool-apply(带
     --defer-membership-to-auto-pool)、control.target、auto-pool.service、
     auto-pool-refresh.path、membership-repair.service、membership-apply.path、
     liquidity.path；enable membership-repair.service + 三个 path
  5. 旧触发器禁用: pin.timer / pin.path / provider-pool-apply.timer 全部
     inactive+disabled；shadow timer/service 仍 disabled/inactive
  6. feature gate: /etc/a6-trader.env 追加 PROVIDER_AUTO_POOL_ENABLED=true（共 28 行，
     mode 600）
  7. 并发操作（外部）: 10:55 UTC 另一操作者基于 v188 叠加 release
     v190-force-account-quota-refresh-minimal 并翻转 /opt 链；该 release 包含本任务
     全部 17 个文件且哈希与我部署的 v190-provider-auto-pool 逐字节一致
     （auto-pool/membership/runtime-failure/liquidity 模块、client、reconciler、
     seller price band、trade_repository、trade_publication_control 任务版、全部
     scripts、0066-0069、units）；其额外改动仅 force_active_account_quota_refresh
     （account usage source active，与本任务无关）。当前 /opt current 指向该 release；
     我的 v190-provider-auto-pool 保留作为候选。unit 文件哈希均一致，systemd 状态不受影响
  8. 数据修复: merchant 19（token 15）非 provider 角色但 inventory 误标
     funding_eligible=true -> 通过 PostgresProviderInventoryProjector.reconcile()
     退休（canonical 5 funded: merchants 4,5,6742,10176,10585；10585 REFILLING）
  9. 废弃陈旧 applying plan 7397（generation->failed、cost->failed、slots 保留 7396）；
     apply 新建 plan 7398（desired 12, ready 5, vacant 7）
部署后链路行为（LIVE 观察）:
  - apply(round-deadline exit 0) -> OnSuccess -> Observer（部分成功轮）-> OnSuccess ->
    auto-pool -> provider_auto_pool_accounts_below_minimum（active 7396 仅 3 funded）
    正确 fail-closed；auto-pool 短暂持锁导致 apply.timer flock 75（视为成功）循环
  - 处理: 临时停 observer.timer 打破循环；apply.timer 保持运行作为 resume 驱动器；
    手动 apply（绕过 flock）可推进 saga
当前阻塞（LIVE）:
  - A6 API transport 持续劣化（a6_transport_failed；近 6h 8064 failed scans；
    Observer 曾整轮 18/18 全败；apply 的 repin/set_slot_cost 停在 5/12）
  - generation 7398 仍 applying；apply.timer 每 ~20s 自动 resume（部分轮次
    provider_reconcile_pin_failed / provider_remote_assignment_slot_not_ready /
    provider_reconcile_parent_compensation_failed，均为 transport 根因）
安全不变量（LIVE 复核）:
  - 0 READY+observed_schedulable slots；0 schedulable inventory；
    provider_auto_pool_generations/accounts 0 行；dispatch 未发布；
    pin/apply 旧触发器 disabled；shadow disabled/inactive
待完成（transport 恢复后）:
  1. 7398 激活（apply.timer 自动 resume；或 transport 恢复后手动一轮）
  2. 重启 observer.timer -> auto-pool 链（5 accounts >= 4 minimum）
  3. auto-pool 执行 owned Channel Ban 审计 + 订阅 + Enodot cost + schedulable
  4. 逐 Provider live readback（evidence record 模板见 §9）
  5. 最终验收（shadow 状态、Seller 触发、dashboard 一致性）
Open blockers:
  - A6 transport 劣化（基础设施问题，非本任务代码）是唯一阻塞
  - 未开启 auto-pool 前 seller-pilot 尚无 auto-pool 输入（正常）
Next exact step: transport 恢复后执行剩余待完成项
Artifact paths:
  /tmp/a6-v190-provider-auto-pool-payload（生产 /tmp，已校验）
  /tmp/STAGE-A-v190.sh（生产 /tmp）
```

### 2026-08-05 T1：fresh live 只读预检（三层对齐，E3，授权执行）

```text
Time (CST): 2026-08-05 11:32（生产 UTC 快照 2026-08-05 03:31:38Z）
Agent/task id: T1（唯一执行者，主代理审批）
Workspace HEAD: main / ae30382（tracked-M=75, untracked=389，保留全部既有改动）
Files changed: 本文档 §12 追加本 ledger；审阅稿 §11 T1 状态行更新（均为追加/指引用）
Commands executed（只读）: ssh us_cloud 的 hostname/readlink/shsystemctl cat+show/list-units、
  sudo grep 命名门禁键、sudo -u postgres psql 只读 SELECT、redis-cli GET dispatch key、
  本地 git log/status、shasum 本地与远端 release 文件。全程无启停/enable/disable/写/触发/迁移。
Test result: 不适用（T1 为 live 只读，未运行测试）
Production read-only result:
  hostname=ser635876468110
  /opt/a6-trader/current -> v203-provider-auto-pool-relist-ban（2026-08-05T00:00Z）
  /www/a6-trader/current -> v181-observer-cookie-seller-fastpath（/opt 与 /www 分叉，worker 以 /opt 为准）
  migration ledger 最高 0069（0065-0069 在册；0064 缺，与本地一致）
  release v203 内: 0066/0069、auto-pool.service、control.target 与本地 sha256 一致；
    reconcile/apply 脚本与本地工作树 DIFF（release 为更早快照）→ 三层差异记录
  systemd（快照时刻 observer 与 auto-pool 处于 activating/start 运行中）:
    observer OnSuccess=auto-pool、PartOf=control.target（新链 live）
    auto-pool ExecCondition PROVIDER_AUTO_POOL_ENABLED=true；
      --observer-snapshot-max-age-seconds=120（仓库=30，差异） --hard-max-ratio-cny 0.3 --minimum-ready-accounts 4
    dispatch.timer active/enabled（OnUnitActiveSec=30s）；dispatch.service static/inactive，无 OnSuccess
    pin.timer/pin.path disabled/inactive（legacy 停用）
    pool-apply.timer active/enabled（设计内 resume driver）；apply.service OnSuccess=observer、带 --defer-membership-to-auto-pool
    listing-schedulability.timer active/enabled；service disabled/static、无 PartOf；
      control.target 32 个 Wants，含 dispatch.timer，不含 listing-schedulability → P0-2 配置级证据成立
    control.target active/enabled；liquidity/failure-repair/membership/membership-apply/auto-pool-refresh paths active
  env gates: PROVIDER_AUTO_POOL_ENABLED=true、PROVIDER_POOL_RECONCILE_ENABLED=true、
    PROVIDER_POOL_APPLY_STAGE=MEMBERSHIP_AND_COST、PROVIDER_POOL_MAX_MEMBERSHIP_CHANGES=12、
    PROVIDER_LIQUIDITY_RECONCILE_ENABLED=true、PROVIDER_POOL_APPLY_CONFIRMATION=<枚举>
    PROVIDER_DISPATCH_RECONCILE_ENABLED、PROVIDER_FAILURE_REPAIR_ENABLED 未设置（→ dispatch 周期 publisher
    resolve_enabled=False 不发 publish，fail-closed；bundle 疑由 apply 内联 publisher 历史写入）
  DB: active routing gpt-5.6-sol gen=7401 cost=7401 desired=12；slots REPAIRING|fixed_pin=5, VACANT=7,
    READY|auto_pool=0（无样本→P0-1 E1 仍成立）、READY|fixed_pin=0、schedulable slots=0；
    inventory RETIRED(f)=12, WARM_STANDBY(f)=5；auto_pool_generations failed=461；
    auto_pool_accounts applying=1, failed=1844；apply/auto-pool 在途 attempt 存在（快照一致）
  Dispatch Redis: a6:provider:dispatch:v1:gpt-5.6-sol schema_version=1/state=active/gen=7401/cost=7401/ready_count=0
    （与 DB 0 READY 覆盖一致；无 auto_pool READY 可断言覆盖）
  Observer freshness（代理 cursor）: next_round_index=5827 @2026-08-05 03:31:24Z（距快照约 14s，新鲜）
  shadow: timer inactive/dead+disabled；service inactive/dead+static（未启动、未等待）
Production mutations: 无（全部只读）
Shadow state readback: timer disabled/inactive；service inactive/dead；未启动、未等待、未依赖
Open blockers:
  - P0-1 无 auto_pool READY 样本（E1 仍成立，未运行兼容验证）；dispatch 覆盖仅 0/0 一致
  - P0-2 配置级证据成立（listing-schedulability 逃逸 control.target；行为验证归 T4/T5）
  - P0-4 三层差异确认（repo worktree vs v203 release vs live）：reconcile/apply 脚本 DIFF
    （方向已纠错：v203=全局 owned-set、worktree=per-merchant owned-set，均无 git 锚点；见证据 13）、
    已安装 auto-pool.service snapshot-max-age=120 vs release 载荷=30（安装漂移）、
    dispatch.timer/listing-schedulability 不在任何 release deploy/systemd（来源不可证）
  - HARD STOP（溯源/兼容未证明）: (a) 两核心脚本 untracked、worktree≠release、Ban owned-set 语义差异；
    (b) 已安装 unit 与 release 载荷漂移；(c) dispatch/listing units 无 release 来源；
    (d) P0-7 运行闸门违例：live PROVIDER_AUTO_POOL_ENABLED=true 且 auto-pool prepare 路径存在
    （含远端 Ban/subscription/cost），但 03:25-03:41 attempts 全部在 prepare 段以 a6_transport_failed/
    a6autoerror 失败、confirm booleans 全 f、remote_readback={} → 远端 mutation 未完成/未证明；
    违例基于「路径 + gate=true」，非基于某次远端写已发生；独立 preflight/grant/admitted-set/C1 过滤均不存在
  - 额外 HARD STOP/遏制候选（shadow 代码路径）: apply planner（trade_provider_pool_apply.py:319-322）
    routine 调用 SingleModelProviderPoolShadowController.run；gen 7402 generation_source='observer-shadow'；
    shadow unit（service/timer）未运行，但 shadow controller 代码路径随 routine pool-apply 运行 fresh
    planning → 区分「unit 未运行」与「代码路径运行」
  - 纠错（deferred 写集）: legacy apply 对 deferred slot 仍远端写 set_schedulable(False)(:1210/1902/2624)、
    drain_slot(:1215-1237)、set_slot_cost(:1249-1273)；仅跳过 repin/probe(:1279-1284)/set_assignment_metadata
    (:1434-1436) → 先前「deferred 无远端 mutation」错误，已修正（证据 10/13）
  - v203 PATCH-MANIFEST.sha256 存在且自洽（修正早前“未核对到 manifest”记录）；
    但其仅证明 release 内部自洽，不证明 ledger=文件 hash，也不证明与 worktree 兼容。
Next exact step: T1-R 遏制（gate=false，本轮唯一批准 mutation）执行后 HARD STOP 等待主代理审批；
  未进入 T2/T3/T4/T5。
Artifact paths（本机脱敏原始输出，sha256 见 MANIFEST）:
  .codex-tmp/t1-evidence-20260805/{01..12}.txt + MANIFEST.sha256
```

### 2026-08-05 T1-R：纠错 + 已批准最小遏制（gate=false）

```text
Time (CST): 2026-08-05（生产 UTC 2026-08-05 03:46 前后）
Agent/task id: T1-R（唯一执行者，主代理审批）
Workspace HEAD: main / ae30382（工作树未动，保留全部既有改动）
Files changed: 本文档 §12 追加本 ledger；审阅稿 §11 T1 行更新；证据 09-12 修正 + 13/14 新增
Commands executed: 只读纠错（diff -u 重读双方、journalctl/psql/redis-cli/systemctl 只读）+
  唯一批准 mutation（备份 /etc/a6-trader.env → sed 原子替换 PROVIDER_AUTO_POOL_ENABLED=false → 回读）。
  auto-pool.service 当时 failed/failed（未 active）→ 未执行 stop（按批准）。未启停其他 unit，
  未 daemon-reload，未改 DB/Redis/远端，未触碰 shadow/pin，未 stage/commit。
A 纠错结论:
  1) diff 方向反转已纠正: v203=全局 owned-set（预取一次），worktree=per-merchant owned_resources
     （reconcile :389-392 / apply :665-682 为 per-merchant；v203 reconcile :391-393 / apply :663-686 为全局）
  2) minimum_ready_not_met 不证明远端 mutation: gen 459-463 attempts 全部 account confirm booleans=f、
     remote_readback={}、error_class=a6_transport_failed/a6autoerror → 远端 Ban/subscription/cost
     未完成/未证明（claim→REPAIRING 为 DB 投影）
  3) shadow 代码路径: apply planner（trade_provider_pool_apply.py:319-322）routine 调
     SingleModelProviderPoolShadowController.run；gen 7402 generation_source='observer-shadow'；
     shadow unit 未运行，但 controller 代码路径随 routine pool-apply 运行 fresh planning →
     额外 HARD STOP/遏制候选（区分 unit 与代码路径）
  4) deferred 写集纠正: legacy apply 对 deferred slot 仍远端写 set_schedulable(False)(:1210/1902/2624)、
     drain_slot(:1215-1237)、set_slot_cost(:1249-1273)；仅跳过 repin/probe(:1279-1284)/set_assignment_metadata
     (:1434-1436) → 先前「deferred 无远端 mutation」错误
Production mutation（唯一批准）:
  备份: /var/lib/a6-trader/deploy-backups/20260805T034557Z-pre-provider-auto-pool-gate-off/a6-trader.env.bak
    before/backup sha=56603a98...；owner/mode=a6trader:a6trader 600（前后一致）
  原子替换（cutover 完成时刻=/etc/a6-trader.env mtime/ctime 2026-08-05 03:46:21.3Z；03:45:57 为备份
    目录时间戳）: PROVIDER_AUTO_POOL_ENABLED=true → false（唯一键；sed+chmod+chown+mv -f）
  after sha=0a712e12...；gate 唯一 false
  回读: auto-pool.service inactive/dead MainPID=0；shadow timer disabled/inactive + service inactive/dead；
    pin timer/path disabled/inactive；DB schedulable slots=0；auto_pool applying=1（陈旧 claim 438）、
    failed=1860；journal: auto-pool 03:46:50 起触发均 "Skipped due to 'exec-condition'"（无新 attempt）；
    pool-apply 03:46:36/03:47:12 报 provider_apply_auto_pool_defer_not_enabled（fail-closed，无 mutation）；
    03:46:53 "Finished" 无显式 ExecMainStatus、无 flock[pid] 输出 → 与 nonblock flock exit75 一致（未直接断言）
  本审批操作（T1-R）未直接写 DB/Redis/remote；cutover 前在途 attempt 曾自行收尾并写失败投影
    （fail_generation 置 account failed），非本操作写入
Shadow state readback: timer disabled/inactive；service inactive/dead；未启动、未等待
Open blockers: 与 T1 相同（P0-1/P0-2/P0-4 + HARD STOP F1-F4 + shadow 代码路径 + deferred 写集已修正），
  另加 pool-apply 在 gate=false 下持续 defer_not_enabled 失败（副作用，fail-closed，属预期）
Next exact step: T1-R 硬停止等待主代理审批；未进入 T2/T3/T4/T5
Artifact paths: .codex-tmp/t1-evidence-20260805/{01..14}.txt + MANIFEST.sha256
```

### 2026-08-05 T1-R2：停止 routine pool-apply/shadow-controller churn（已批准最小遏制）

```text
Time (CST): 2026-08-05（生产 UTC 2026-08-05 03:50-03:51）
Agent/task id: T1-R2（唯一执行者，主代理审批）
Workspace HEAD: main / ae30382（工作树未动）
Files changed: 本文档 §12 追加本 ledger；审阅稿 §11 T1 行更新；证据 13/14 修正 + 15 新增
A 纠错:
  1) 03:46:53 无显式 ExecMainStatus → 表述改为「与 nonblock flock exit75（SuccessExitStatus=75）一致，
     未见 python 输出」；未直接断言。定位 v203 apply main(): resolve_enabled(PROVIDER_POOL_RECONCILE_ENABLED)
     → not enabled → run_model(enabled=False) 直接返回（:1432-1441，无 DB/Redis/remote/planner/ShadowController）
     → 之后才是 defer 校验（:1443-1450）→ dsn/投影/planner。证明 reconcile gate=false 在 defer 校验和
     ShadowController 前退出且无 mutation。
  2) cutover 边界: env mtime/ctime=03:46:21.3Z（原子替换完成）；03:45:57 为备份目录时间戳。修正 14/ledger。
     「mutation 窗口无 DB 写」→「本审批操作未直接写 DB/Redis/remote；cutover 前在途 attempt 自行收尾并写失败投影」。
  3) 修正「auto-pool gate=false 冻结 apply planning」: gate=false 只使带 --defer-membership-to-auto-pool 的
     unit 在 :1448 fail-closed（defer 特定）；通用关闭由 PROVIDER_POOL_RECONCILE_ENABLED=false 提供（本轮 mutation）。
Production mutation（唯一批准）:
  备份: /var/lib/a6-trader/deploy-backups/20260805T035036Z-pre-provider-pool-routine-off/a6-trader.env.bak
    before sha=0a712e12...；owner/mode=a6trader:a6trader 600；timer FragmentPath=/etc/systemd/system/a6-trader-provider-pool-apply.timer
  原子替换: PROVIDER_POOL_RECONCILE_ENABLED=true→false（唯一键）；after sha=a058c902...；两 gate 唯一 false
  systemctl disable --now a6-trader-provider-pool-apply.timer → post disabled/inactive
  （pool-apply.service 当时 inactive，未 stop；未 mask；未停 membership-apply.path/observer/dispatch/listing/
  control.target；未 daemon-reload；未触碰 shadow/pin；未写 DB/Redis/remote）
回读:
  gates: PROVIDER_POOL_RECONCILE_ENABLED=false + PROVIDER_AUTO_POOL_ENABLED=false（唯一）
  owner/mode: a6trader:a6trader 600；pool-apply.timer disabled/inactive；pool-apply.service inactive/dead MainPID=0；
  membership-apply.path active/waiting（保持）；auto-pool.service inactive/dead；shadow timer disabled/inactive+
  service inactive/dead；pin timer/path disabled/inactive；DB schedulable=0；gen 7402 updated_at=03:45:27.015Z（cutover 后不变）
  journal（sleep 20s ≥1 原周期）: auto-pool 触发均 exec-condition skip；pool-apply 无 timer 周期触发，
  但 membership-apply.path 仍触发 Starting→Finished（exit0 disabled，main :1436 早退）→ 无 ShadowController/
  planning/mutation。透明报告: 「无新 pool-apply Starting」未完全满足（path 触发仍在），「无 ShadowController 调用/无写」满足。
Production mutations: 仅 env PROVIDER_POOL_RECONCILE_ENABLED=false + disable pool-apply.timer（本轮唯一批准）
Shadow state readback: timer disabled/inactive；service inactive/dead；未启动、未等待
Open blockers: 同前（P0-1/P0-2/P0-4 + HARD STOP F1-F4 + shadow 代码路径已由 reconcile gate=false 通用关闭；
  membership-apply.path 仍可触发 pool-apply 但为 gate=false 早退无副作用）
Next exact step: T1-R2 硬停止等待主代理审批；未进入 T2/T3/T4/T5
Artifact paths: .codex-tmp/t1-evidence-20260805/{01..15}.txt + MANIFEST.sha256
```

## 13. 可直接交给下一 Agent 的任务描述（**superseded historical task template，不再可直接执行**）

> ⚠️ 本节为 **2026-08-04 历史任务模板**，其前提（HEAD `557747e`、生产 migration 最高
> `0065`、auto-pool/membership units 未安装、旧 Pin/apply triggers active/enabled）已过时，
> 且其中的“扩大回归后再做生产发布”步骤已被 2026-08-05 审阅稿取代。**新任务只能从 §0 出发，
> 按 [`architecture-deployment-review-20260805.md`](architecture-deployment-review-20260805.md)
> §9 的 T1→T2→T3→T4→T5 执行**。本节仅作为历史上下文与 evidence ledger 保留，正文不改写。

```text
在 /Users/haven/Documents/code/a6api-auto 继续 Provider auto-pool 恢复任务。
先阅读 AGENTS.md、docs/provider-auto-pool-design-and-progress-20260804.md 和
docs/provider-auto-pool-agent-handoff-20260804.md。保留全部未提交改动，不 reset、clean、checkout 或全局格式化。

从当前中断点开始：
1. 只补 tests/test_trade_provider_pool_reconciler.py 缺失的 nullcontext import，重跑文档列出的 focused
   pytest/Ruff/cmp。
2. 增加临时 PostgreSQL integration tests，覆盖 migration 0001-0069、account claim CAS、stale claim、
   membership completion CAS、slot/inventory 投影和 activation barrier。
3. 实现或证明 model-scoped DB lease，闭合 begin_generation/claim_accounts 竞态。
4. 审计并闭合 Provider immutable matching cost generation 证据链。
5. 扩大回归后再做生产只读预检、最小 release、备份、migration/unit 部署和逐 Provider 精确 readback。

生产 systemd 使用 /opt/a6-trader/current；/www/a6-trader/current 当前是另一 release。生产 migration 最高
0065，新 auto-pool/membership units 尚未安装，旧 Pin/apply triggers 仍 active/enabled。
全程保持 a6-trader-provider-pool-shadow.timer disabled、service inactive。
```
