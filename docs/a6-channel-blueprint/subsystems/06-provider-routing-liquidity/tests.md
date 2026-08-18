# S06 Provider 路由与流动性：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| RTE-T001 | RTE-001 | route owner/failure entry | 初始/admin/staged-token route 全命中 S06；Auto-Pool PUT 前 gate READY，Pin 进入 schedulable 前 gate READY；S05 route mutation spy=0，S06 不保存 gate 内部状态。 |
| RTE-T002 | RTE-002,003 | `next_complete_band` | fresh snapshot 严格选择当前边界后的下一 Decimal 价带并完整包含同价项。 |
| RTE-T003 | RTE-002,003 | expand activation | pause < 单次 full gate < PUT < subscription exact readback/generation fence < route+cost activation < restore；gate 后 membership 变化不恢复；post-PUT full-gate spy=0。 |
| RTE-T004 | RTE-004 | `maybe_shrink_auto_pool` | 四个稳定条件和二选一统计阈值全满足才 target-1；相等价带不截断。 |
| RTE-T005 | RTE-005 | range exhausted | >max_n、无 candidate、下一带>.3 各自摘流；60 秒后 fresh 合法带可恢复。 |
| RTE-T006 | RTE-006 | `resolve_route_unknown` | PUT 响应丢失后 target/old/third 三状态分别 adopt/replay/replan，期间始终 paused。 |
| RTE-T007 | RTE-007 | `reconcile_repin` | Pin 失败不扩 target_n；五类结果闭合；pre-activation gate、assignment non-owned、channel/S04 cost+capability/model 任一缺失不恢复。 |
| RTE-T008 | RTE-008 | `handle_provider_runtime_failure` | 所有 insufficient quota 变体只进入 UNFUNDED/liquidity，Observer 未被调用。 |
| RTE-T009 | RTE-009 | `reconcile_liquidity` | callback witness + immutable transfer intent 在 POST 前存在；响应丢失后 target/old/third/incomplete 不重复新 amount；amount=0 路径 readback 有定义；仍不足才 duty 移除、slot VACANT。 |
| RTE-T010 | RTE-010 | `admit_replacement` | funding/BAN_INIT/current gate/range/cost/capability/subscription/schedulability 任一缺失时 replacement 在 slot 外。 |
| RTE-T011 | RTE-011 | callback selection | Token 三资源隔离；无 listed+callable small Channel 时记录 skip，balance transfer POST spy=0；callback Pin unknown/reject 时同样无 transfer。 |
| RTE-T012 | RTE-012 | `run_liquidity_callback` | 请求 input 字节精确；高缓存 realistic usage 通过；`prompt_tokens=1/completion_tokens=0/total_tokens=1` 或其他 probe-shaped usage 不计 transfer。 |
| RTE-T013 | RTE-013 | `maybe_run_provider_shadow` | 有 reusable generation 时不运行；无合理 route change 时拒绝；one-shot 前后 timer disabled。 |
| RTE-T014 | RTE-014 | Seller dependency | Seller apply 延迟/失败不阻止已满足自身 readback 的 Provider；事件仍可靠投递。 |
| RTE-T015 | RTE-001,002,007 | gate cost regression | 每个 Auto-Pool PUT 恰有一个 pre-PUT full gate、零 post-PUT full scan；所有关系已禁用时 disable mutation=0。Pin 配置后仅在 schedulable activation 运行一次 gate；Seller price/request 的 gate spy=0。 |

## 2. 属性与竞态

- 对随机价格带断言结果严格大于旧 boundary、<=0.3、完整价带且 included_count<=max_n。
- quota 与非-quota 分类对所有 `errorlist.md` 变体互斥。
- 并发两次 expand 只有 current generation operation 可 PUT；旧 operation superseded。
- PUT 后任意 crash 不会把旧 route 当作确认事实。

## 3. 生产验收证据

保存一次隔离 Auto-Pool readback 或复用 generation 的证据、一次 Pin readback、一次无损 liquidity dry-run/受控 transfer、callback usage witness，以及 `a6-trader-provider-pool-shadow.service` 和 timer 未启用的前后状态。生产失败演练不得伪造 quota 或发送额外显眼 Probe。

## 需求覆盖索引

本测试集覆盖：`RTE-001`, `RTE-002`, `RTE-003`, `RTE-004`, `RTE-005`, `RTE-006`, `RTE-007`, `RTE-008`, `RTE-009`, `RTE-010`, `RTE-011`, `RTE-012`, `RTE-013`, `RTE-014`。
