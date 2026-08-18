# S04 Class1/2/3 输入账号：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| ACC-T001 | ACC-001 | `classify_account` | 三类仅由显式 binding 分类；伪装名称/role/group 不改变类别。 |
| ACC-T002 | ACC-002,003 | `observe_auto_pool_ratios` | target_n=10 时 sk=r10，所有与 r10 相等项都纳入；M<3 range exhausted。 |
| ACC-T003 | ACC-003 | Top-N boundaries | target/min/max/hard max 使用 Decimal，重复价格带不会按 max_n 截断。 |
| ACC-T004 | ACC-004 | `confirm_class1_cost` | 无 current 时第一份合格快照建立 generation 1；上涨首个新 snapshot 生效；下降前两个 distinct snapshot 保留旧值，第三个才切 generation；同 snapshot replay 不重复计数。 |
| ACC-T005 | ACC-004 | Class1 formula | realized p95、retry reserve、5/6 输入变化不影响 sk。 |
| ACC-T006 | ACC-002 | `pin_cost` | 只接受 current assignment epoch 精确 channel/model/K_CNY；过期 epoch 拒绝。 |
| ACC-T007 | ACC-005 | `compute_class2_sk` | used=50/utilization=.5/multiplier=2/account cost=20 得 sk=.1，Decimal 精确。 |
| ACC-T008 | ACC-006 | `compute_class2_sk` | utilization=1 或 >1 不暂停/刷新/替换账号。 |
| ACC-T009 | ACC-007 | `compute_class2_sk` | 矛盾字段保留历史；无历史用 one_week_A=100 并标 estimated。 |
| ACC-T010 | ACC-008 | `class3_sk` | 模型 override 优先，其他模型使用 declared_sk；provider_code 类别稳定。 |
| ACC-T011 | ACC-009 | `evaluate_class3_window` | 样本19或expected<1不合格；5%、15%、30%精确边界按合同处理。 |
| ACC-T012 | ACC-009 | bad streak | 连续三合格窗>15%暂停；中间<=15%重置；单窗>30%立即暂停。 |
| ACC-T013 | ACC-010 | `recover_class3` | 只有三连续合格窗<=5%生成新 billing generation；预算不足仍暂停。 |
| ACC-T014 | ACC-011 | `effective_profit` | 账号 override > provider default > class default，三类默认值精确。 |
| ACC-T015 | ACC-012,013 | probe scheduler | input usage 499/3001 拒绝；预算耗尽只延期且不产生 allowlist。 |
| ACC-T016 | ACC-014 | `project_account_schedulability` | native/cost/capability/billing/BAN_INIT 任一失效则不 ready；满并发不改变 structural ready。 |

## 2. 属性测试

- 随机 Decimal `used/utilization/multiplier/cost` 与高精度参考公式比较。
- Top-N 对排序、重复价带、M/N 边界和极小比率做 permutation invariant 测试。
- Class3 窗口事件乱序/重复时按 request identity 去重，结果稳定。

## 3. 现有能力回归

Class2 utilization 到 100% 及以上时，运行现有 OAuth refresh/status 测试，证明 S04 不注入新生命周期动作。Class2Adjustment 的 API、规则文件和行为由 S10/S16 回归，本包不得修改。

## 4. 验收证据

输出每类账号的 cost/capability/billing generation 示例、公式输入、Decimal 序列化和 schedulability reason。凭据只显示 reference/fingerprint。

## 需求覆盖索引

本测试集覆盖：`ACC-001`, `ACC-002`, `ACC-003`, `ACC-004`, `ACC-005`, `ACC-006`, `ACC-007`, `ACC-008`, `ACC-009`, `ACC-010`, `ACC-011`, `ACC-012`, `ACC-013`, `ACC-014`。
