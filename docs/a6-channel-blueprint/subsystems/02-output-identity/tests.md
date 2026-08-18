# S02 出口身份、用户与绑定：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| OUT-T001 | OUT-001,002 | `stage_output_identity` | 创建 inactive `a6-channel`、稳定 Channel 和 DISABLED/STAGED Key；第二 Channel 或 effective Key 被拒绝。 |
| OUT-T002 | OUT-003 | `audit_toa6api_group` | 输出用户只在 `ToA6API` 且倍率 1.0；普通用户加入或倍率漂移会关闭受影响准入。 |
| OUT-T003 | OUT-004 | `authenticate_output_request` | 负余额请求仍可过余额门，但 Key/IP/RPM/模型/eligibility 继续执行。 |
| OUT-T004 | OUT-005 | auth limit path | `concurrency=0` 不限并发但 `quota>0` 仍执行用户 quota 限制；`quota=0` 不限用户 quota 但 `concurrency>0` 仍限并发；两种情况都跳过余额 reserve/平台级 quota accounting。 |
| OUT-T005 | OUT-006 | login/key APIs | 普通登录、自助注册、用户端 Key create/update/delete 对 `a6-channel` 均拒绝。 |
| OUT-T006 | OUT-007 | `resolve_listing` | 精确 model_no 唯一解析；合法单 alias 解析；alias 碰撞进入 BINDING_REVIEW 且不调度。 |
| OUT-T007 | OUT-008 | `list_output_models` | 只返回该 Channel 当前 OPEN bindings；CLOSED、其他用户和普通组模型不出现。 |
| OUT-T008 | OUT-009 | `project_output_candidates` | Class1/2/3 只由显式 binding 投影；改变 group multiplier 不改变经济值且被 drift audit 拒绝。 |
| OUT-T009 | OUT-010 | `build_dispatch_context` | 每种 endpoint/stream 解析稳定 listing 与 transport profile；未指定/未知 model 在 scheduler 前失败。 |
| OUT-T010 | OUT-011 | `activate_staged_output` | 任一 Binding/BAN_INIT/price/capability/eligibility gate 缺失时用户和 Key 保持 inactive；全通过才原子激活。并发在 gate check 与 commit 间切换 Ban membership/price/eligibility generation 时，旧 expected generation 必须失败，不能激活 stale gate vector。 |
| OUT-T011 | OUT-012 | `audit_toa6api_group` | cutover 后仅有旧 `TPA6API` 时不得 fallback；产生明确 config failure。 |
| OUT-T012 | OUT-002 | concurrent key activation | 两个 staged Key 并发激活最多一个 effective，失败事务不短暂双启用。 |

## 2. 请求面矩阵

对 Responses/Chat、stream/non-stream、精确模型/alias、余额为 0/负数、concurrency/quota 四种零值组合运行集成矩阵。每次断言 stable `listing_binding_id`、transport profile、output/listing generation 和候选 projection hash 一致。

## 3. 隔离与回归

- 普通用户的余额 reserve、quota、登录、Key 管理和 `/v1/models` 行为保持原样。
- 一个 Listing hold 只关闭该模型，不禁用同 Channel 的其他 Listing。
- Redis 丢失时从数据库 current projection 保守失败，不按 group name 临时构造候选。
- 用户 inactive 或 Key 非 effective 时，无论 Listing projection 多新都不能派发。

## 4. 验收证据

保存用户/Channel/Key/Listing 唯一约束查询、`ToA6API` 成员审计、负余额成功鉴权和普通用户回归报告；报告只显示 Key fingerprint/last4。

## 需求覆盖索引

本测试集覆盖：`OUT-001`, `OUT-002`, `OUT-003`, `OUT-004`, `OUT-005`, `OUT-006`, `OUT-007`, `OUT-008`, `OUT-009`, `OUT-010`, `OUT-011`, `OUT-012`。
