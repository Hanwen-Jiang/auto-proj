# S07 价格事实、ck 与 FX：测试规格

[自然语言](logic.md) | [伪代码](pseudocode.md) | [总纲](../../README.md)

## 1. 核心用例

| Test ID | 需求 | 被测符号 | Given / When / Then |
| --- | --- | --- | --- |
| PRF-T001 | PRF-001 | public/net conversion | 任意 Decimal ck 的公开倍率精确为 `ck*1.2`，不再额外扣 commission。 |
| PRF-T002 | PRF-002 | FX/ck formula | CNY/USD ratio 与 canonical micros 可正反复算，零/负输入拒绝。 |
| PRF-T003 | PRF-003 | `choose_canonical` | 按 input/output/cache_read/cache_write 首个非零；全零触发 config invalid。 |
| PRF-T004 | PRF-004 | `target_listing_vector` | 每个非零维度独立 ceil；不存在四舍五入或 float 漂移。 |
| PRF-T005 | PRF-004,005 | `confirm_listing_vector` | remote vector 必须逐维等于精确 ceil expected_vector；ck_d 落在 `[target_ck,target_ck+one_micros_ck)`，canonical 索引正确、confirmed_ck=min；额外/不足 1 micros 的远端漂移都失败。 |
| PRF-T006 | PRF-006 | `audit_output_config` | markup 缺失、重复、非法、19.999/20.001、source 异常都被收集为 errors 并持久化 `OUTPUT_CONFIG_INVALID`，不得因 `require` 提前退出而漏建 hold；完整修复 readback 后只清匹配 S07 source。 |
| PRF-T007 | PRF-006,007 | config drift | charge type/billing/official vector/remote price 任一漂移被检测；修复读回后才清。 |
| PRF-T008 | PRF-008 | base resolver | `ToA6API` 使用 A6 vector；普通 group 仍走原 resolver，结果不被全局覆盖。 |
| PRF-T009 | PRF-009 | write outcome | CONFIRMED 仅一个 S07 confirmed generation；old/third/reject/incomplete resolution 原样返回 S08，均保留 last_confirmed 且 desired 不成为 observed。 |
| PRF-T010 | PRF-010 | `resolve_fx` | A6 与 fallback 同时可用时只选 A6；A6 恢复立即切回，不平均。 |
| PRF-T011 | PRF-011 | fallback policy | 3s timeout、30m cache、15m refresh、96h source age 边界精确。 |
| PRF-T012 | PRF-011 | both sources down | 返回 LAST_CONFIRMED 可结算旧 attempt，但 `allow_new_price_write=false`。 |
| PRF-T013 | PRF-012 | generation persistence | official model-price、FX、Listing-price 三类 generation 都保存原始整数/value、canonical、source hash/time 和 rounding policy；相同 source/payload replay 不创建重复 current generation，任一历史 generation 可离线重算相同 ck。 |

## 2. 属性测试

随机生成官方 micros、目标 ck 与 FX，验证远端 vector 每维是满足目标的最小整数，`micros-1` 必低于目标（官方非零时）。对极大 micros 和小 FX 使用 arbitrary precision，禁止溢出。

## 3. 生产前合同测试

用 A6 read-only platform models、markup-preview 和 Listing readback 形成一组真实脱敏 fixture；用 mock Frankfurter 验证 stale/timeout。生产不为价格事实测试单独写价，写入由 S08 隔离 operation 触发。

## 需求覆盖索引

本测试集覆盖：`PRF-001`, `PRF-002`, `PRF-003`, `PRF-004`, `PRF-005`, `PRF-006`, `PRF-007`, `PRF-008`, `PRF-009`, `PRF-010`, `PRF-011`, `PRF-012`。
