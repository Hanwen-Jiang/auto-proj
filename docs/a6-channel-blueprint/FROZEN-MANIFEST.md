# A6 Channel Canonical Design 冻结清单

冻结时间：2026-08-13（Asia/Shanghai）

Canonical 文件：[A6API_Canonical_Design_FrozenCandidate_SSEIntegrated.md](../A6API_Canonical_Design_FrozenCandidate_SSEIntegrated.md)

Canonical SHA-256：

```text
FF86555B860D6C3EEC05690718C46A4CEA9E04AEC5F8D448A55DA987ED9EEF19
```

## 1. 冻结含义

- 下表 61 个源文件是权威编辑面；Canonical 是它们的只读聚合，已验证 `markers=61`、`reverse_diffs=0`。
- 冻结稿只允许因用户明确授权、当前硬规则直接冲突、已验证 API/事实错误或可复现正确性故障而修改。
- 审计默认只读。新的建议、边缘情形或“更生产级”不是修改授权。
- 横向 R0-R4 只能投影冻结稿能力，不得反向修改冻结稿或新增平行业务模型。
- 任一获准修改必须更新受影响源文件、重新生成 Canonical、更新本清单全部哈希，并记录最小 delta 与原因。

## 2. 本次保留与回退判定

| 判定 | 内容 | 理由 |
| --- | --- | --- |
| 保留 | authoritative owned set 包含完整 Supplier inventory 中仍存在的 active/down/unavailable/retired/hidden 项，以及本地仍受控且无精确远端删除证明的项 | 根 `AGENTS.md` 明确要求，防止远端仍可见 owned Channel 被 Provider 选中形成自路由 |
| 保留 | Provider Token onboarding、owned Channel create/adopt/restore、Provider 进入/恢复 schedulable、Auto-Pool `max_ratio` 更新、SelfRoute repair 运行 S05 Exclusion Gate | 这些事件能改变 owned 集合、远端可路由状态或 Auto-Pool 候选集合 |
| 保留 | 固定完整分页参数、逐项 `user_channel_disabled=true && route_status=user_disabled`、effective subscription 非空且无 owned、membership generation fence | 是根硬规则的直接验收条件 |
| 保留 | 复用 `provider_listing_ban_relations`、`ban_membership_generation` 与现有 operation journal/audit | 已有状态足以承载关系、并发与证据，不需要第二套模型 |
| 回退 | `provider_owned_channel_exclusion_attestations` 独立业务表 | 与现有 BanRelation/operation audit 重叠，增加 schema、生命周期和恢复负担 |
| 回退 | parent/child attestation、`EXCLUSION_CONFIRMED`/`SCHEDULABLE_READY` 双阶段状态机 | 把局部安全门扩成第二业务状态机，收益不足以抵消复杂度 |
| 回退 | token-set、inventory、owned-set 多层 hash 与跨子系统 generation 传播 | `ban_membership_generation` 与当前完整读回已经能阻断并发变化；额外 hash 扩大耦合 |
| 回退 | S02/S04/S08/S09/S13 等无关子系统保存或传播 Ban 内部证明 | 只需由 S05 返回 READY/阻断，S06/S16 在 owning operation 内消费 |
| 回退 | gate 前后双全量扫描、周期扫描、请求热路径扫描 | 增加远端调用、延迟与故障面；当前 generation fence 足以在提交前发现成员变化 |
| 不触发 | Seller Listing 改价、普通请求、cooling、Listing-scope create/adopt/takedown/resubmit | 不改变父 Channel owned 集合或 Auto-Pool `max_ratio`；新 Listing 只执行 scoped `LISTING_BAN_INIT` |

## 3. Exclusion Gate 成本上限

每个触发 operation 最多执行：

1. 一次完整 Supplier inventory。
2. 每个受影响 Provider Token/model 一次完整 Marketplace 分页。
3. 只对读回漂移的关系执行 disable；已禁用关系零 mutation。
4. Auto-Pool PUT 后一次 effective subscription 精确读回。
5. 提交前一次 `ban_membership_generation` 比较。

禁止新增 attestation 表、parent/child 状态机、token-set/owned-set hash、前后双全量扫描、周期扫描或请求热路径扫描。突破此边界须按用户级 `OVERENGINEERING_VIOLATION` 规则单独证明收益、性能成本、实现成本、运维成本与更简单方案为何不足，并取得明确授权。

## 4. 权威源文件哈希

| 源文件 | SHA-256 |
| --- | --- |
| `docs/a6-api-reference.md` | `1E8DC0A8D4BD7C27B8AE78FBDF72466DF0D51BCC09D5D4E9117405388754245E` |
| `docs/legacy-trader-logic.md` | `DC4B42954EB833A2539994F373148BA4546ECCD9B2D9638AA1587531230E31E7` |
| `docs/sub2api-a6-channel-implementation-prompt.md` | `10D7BC0E59F77179C04693E266EDE10F8BC7BDDB44E7B5E310B00F095A7D5A8C` |
| `docs/a6-channel-blueprint/README.md` | `25CFAF6BA2DBA1F720B51CB0C500768CB6E2E34E00DCA448A7F4DDEE04D26D87` |
| `docs/a6-channel-blueprint/architecture.md` | `ED360C5D007DEA15D32EF777D33E21DC8E0E05112F9718A91E1791F74A8F1914` |
| `docs/a6-channel-blueprint/traceability.md` | `B3A171F7CB3E960B1BAC3B2ADFF3FD6086EFB9F407B4912596CDDFDFC2BBB373` |
| `docs/a6-channel-blueprint/subsystems/00-foundation/logic.md` | `112AC23DF23FC8E3B527D1F6684C47606F60D6FD8A9F51271E007A5EDBDAA3AB` |
| `docs/a6-channel-blueprint/subsystems/00-foundation/pseudocode.md` | `9057DE6EF598DCCABA89CFF05DDE254E9DED3C0DF65487CF5486DB31C12A966D` |
| `docs/a6-channel-blueprint/subsystems/00-foundation/tests.md` | `30D4EF528B401296F47FD2A68F4019E39D746BD4DDBC9581CD8A9185D9037806` |
| `docs/a6-channel-blueprint/subsystems/01-a6-connector/logic.md` | `623C66E217C935853D9288D2C0278B854BA8C440B55F968B6999B07CD73E2372` |
| `docs/a6-channel-blueprint/subsystems/01-a6-connector/pseudocode.md` | `9FFBA810923802281BEE915776A9F9DC039A9E11D735E991A0B69100FBD903EC` |
| `docs/a6-channel-blueprint/subsystems/01-a6-connector/tests.md` | `8575D046C7CFE28DDAB59B3EEE6B5333A3A6AB09F851036ABAA71C52FE660B5F` |
| `docs/a6-channel-blueprint/subsystems/02-output-identity/logic.md` | `AEF14CED225992CCACF837E14AA45D0D59156FAFA60775C2F827DEE1F60C7B4F` |
| `docs/a6-channel-blueprint/subsystems/02-output-identity/pseudocode.md` | `89B638084AEEB94A28D6ECA92C154945BA20201684DD3A3096929B51FD0D1F73` |
| `docs/a6-channel-blueprint/subsystems/02-output-identity/tests.md` | `30E75095FCC77D9490DF426F0E0FB391C8570C2CF60A70D52715FF9CEDF082C8` |
| `docs/a6-channel-blueprint/subsystems/03-output-lifecycle/logic.md` | `C1A75A6D3491F9850CE6C7D051ED7D7B4222951A6540B7F1413E7ED0AEBFE91A` |
| `docs/a6-channel-blueprint/subsystems/03-output-lifecycle/pseudocode.md` | `DD1A58DB8E14FF060F3A5D2597F5A229F72701DBEB1690A451A47FE955A895D4` |
| `docs/a6-channel-blueprint/subsystems/03-output-lifecycle/tests.md` | `13D4D2A259BB3E4F6D2B08743C4BC95051A32E5ABEF740687A888C83DF75E51F` |
| `docs/a6-channel-blueprint/subsystems/04-input-accounts/logic.md` | `893DF001EE1287AF93054DC75E5A11999A8720B471AE0C477417BD1987C9EC16` |
| `docs/a6-channel-blueprint/subsystems/04-input-accounts/pseudocode.md` | `4673CC513C7767AEC62D7E33E80D4FB9258B2AA9F59F483628FA56D4F6302C39` |
| `docs/a6-channel-blueprint/subsystems/04-input-accounts/tests.md` | `D02F639799156DA450190356CDA9C3FBE6205ABB229A8DED62B23512968C8F6A` |
| `docs/a6-channel-blueprint/subsystems/05-provider-token-ban/logic.md` | `28383BCA25C807A07469863F65708A946BB3089F176F81767145A420C035F29A` |
| `docs/a6-channel-blueprint/subsystems/05-provider-token-ban/pseudocode.md` | `08DF98545BFB221BA4BB5588CF8568960C7F54C61ED09E860D1E0A21E603FB8E` |
| `docs/a6-channel-blueprint/subsystems/05-provider-token-ban/tests.md` | `7BD7A99131853AAF12E61A58E2A1C51F6A9EDE3D8F756F4B7A61B491E082802F` |
| `docs/a6-channel-blueprint/subsystems/06-provider-routing-liquidity/logic.md` | `6347F7F645E2CAEB1D74A6A79F5A5E143BD7118422D9D637499FC220B0B7447C` |
| `docs/a6-channel-blueprint/subsystems/06-provider-routing-liquidity/pseudocode.md` | `6AE002D3A21B70EE8E99BF170C397454B736792AFEEDF776D2B7D66415FFE029` |
| `docs/a6-channel-blueprint/subsystems/06-provider-routing-liquidity/tests.md` | `0D9F808A5A2A29BE6B018FBD0DE9B1E2A8B6DBBB696C7BD13F013892CD451852` |
| `docs/a6-channel-blueprint/subsystems/07-price-facts/logic.md` | `7129319CDA95F65642DB67706A4FD38C72DEC8F0C6C7681027B65B57EE1DC7F9` |
| `docs/a6-channel-blueprint/subsystems/07-price-facts/pseudocode.md` | `06FC680857BE9E9AD89A2C97E990057F0CC24E99B37CE160E77CE1F42D85A4EB` |
| `docs/a6-channel-blueprint/subsystems/07-price-facts/tests.md` | `3FBB8331453AB183B69E299F920444C6420580446EF17D27E98C2F905E89CB39` |
| `docs/a6-channel-blueprint/subsystems/08-pricing-policy/logic.md` | `2A2F35271ED536D9CB9751991777902AD97877D2E64F19DF6D39BBAB56D8C739` |
| `docs/a6-channel-blueprint/subsystems/08-pricing-policy/pseudocode.md` | `56DF755E57802E06BE67DD4C9D4CD6E524ADB0CB9C6F42C3A561790D3F0009A5` |
| `docs/a6-channel-blueprint/subsystems/08-pricing-policy/tests.md` | `E515AEBBAE510741A9742D7B4FDF23B4033571C43314B0501F3EF6A8470986FD` |
| `docs/a6-channel-blueprint/subsystems/09-eligibility-delta/logic.md` | `7DF435B26D8F5400DEC1E540AAF45183D5BEB833C14AA5BC4B19A72E89B3F27E` |
| `docs/a6-channel-blueprint/subsystems/09-eligibility-delta/pseudocode.md` | `21165CC716FB5786B705EDF5B1B957C2CF28222B09DE99C7E09DF8B12A424403` |
| `docs/a6-channel-blueprint/subsystems/09-eligibility-delta/tests.md` | `CC6C8BCF729EEB2BFC66C9521C58A85986B417B64D32BEBAAF99FE4CB79674B4` |
| `docs/a6-channel-blueprint/subsystems/10-usage-billing/logic.md` | `DFE67BCD85430D1C87B387C676269EC1CA341F97AFD07D13E459508500B7EBDA` |
| `docs/a6-channel-blueprint/subsystems/10-usage-billing/pseudocode.md` | `30224AB39724DD002F2CB3303F54518FDF94DAC43683CB0C08BB49F5C55648EE` |
| `docs/a6-channel-blueprint/subsystems/10-usage-billing/tests.md` | `47403C0A20EFD8A52F4FFA47AACC8B34B473CBD2EDB68FE652F95DC11FABC9E3` |
| `docs/a6-channel-blueprint/subsystems/11-revenue-replay/logic.md` | `BCFC6FD84E3FF3DCF5FAB1EDF2055A5D3D8A799F823A40AEB7D350B6920CFFCF` |
| `docs/a6-channel-blueprint/subsystems/11-revenue-replay/pseudocode.md` | `8B6D8FC5D6D7B91628006CFC93B2AA09A85CD00727D45B8281BE3629ACD78917` |
| `docs/a6-channel-blueprint/subsystems/11-revenue-replay/tests.md` | `71627C07FE8EE0CB236A8B120794FF4E2D79D4926BB9CD722EFCAF9E70AB249C` |
| `docs/a6-channel-blueprint/subsystems/12-scheduling-incidents/logic.md` | `2B2D0DA0AD7EF6D3AA78D9F55F6F81B130E54541C6911D83E65F1F49C6865E3D` |
| `docs/a6-channel-blueprint/subsystems/12-scheduling-incidents/pseudocode.md` | `D417F844C52D9E21058AA7CE4CFC01ADC96D96D48C748EF1594E2E2335549705` |
| `docs/a6-channel-blueprint/subsystems/12-scheduling-incidents/tests.md` | `A298D0CE8F632118D58F6C5793D99CE1F21F000F6F6469C9CAA95D5FE2870188` |
| `docs/a6-channel-blueprint/subsystems/13-listing-recovery/logic.md` | `786C41428B35B443BE49A72A96D469D81FD1D95D71AE9EF714B584C6C7E1B05F` |
| `docs/a6-channel-blueprint/subsystems/13-listing-recovery/pseudocode.md` | `B05510542D21F43D3ACBA9FA8BBCB14625A8A81ED5527AA6983F759297FF053F` |
| `docs/a6-channel-blueprint/subsystems/13-listing-recovery/tests.md` | `A377EEC8A78330BF2C0A5A73F96EAF18417FF05190F4C119B26D2142AD9A6B93` |
| `docs/a6-channel-blueprint/subsystems/14-async-control/logic.md` | `CE32593BAA3E0028FDD00A47F16D1BE8DCB16CA55C8D43E714AD4BF565B5ADFF` |
| `docs/a6-channel-blueprint/subsystems/14-async-control/pseudocode.md` | `6CA947C570DE301B0EE5D430674BD311B9157D5B294559893E2E9838AC22BB30` |
| `docs/a6-channel-blueprint/subsystems/14-async-control/tests.md` | `9C1B816B01383DCD5E3AC16A3D5E93B8C5622774E7A06EB86F2DC0BC59880B2D` |
| `docs/a6-channel-blueprint/subsystems/15-admin-console/logic.md` | `7927465DA0721B467CAE5EB19B0E0779EDD233950C56E868808FB853B8E324F1` |
| `docs/a6-channel-blueprint/subsystems/15-admin-console/pseudocode.md` | `8D54A45E6D4776DAB35455447434314575E98BD94B136AFB74A8BEED259BF1DC` |
| `docs/a6-channel-blueprint/subsystems/15-admin-console/tests.md` | `F6F5D3ED384107C365DE2552BD6B64CB6976FEB127648FEB4039378E5FF1672F` |
| `docs/a6-channel-blueprint/subsystems/16-migration-rollout/logic.md` | `44236BBBCE69E21CABE00DAC4D4942A355D164AAAA6DF444BC64CB8EA8174B17` |
| `docs/a6-channel-blueprint/subsystems/16-migration-rollout/pseudocode.md` | `AF80388D845159E42FEF26FB5D22AD25E6F5656DC72198AF1D2B417BD000DBCE` |
| `docs/a6-channel-blueprint/subsystems/16-migration-rollout/tests.md` | `A5AA2F09930F980A522C4F0078C78A4C3C18D0C3A8413258774C7088D9C203AC` |
| `api-docs/7-24-15-54.md` | `5083081E241B90D5DC00B33F922838F429DFD58D9D962049B90F1728EE2B4CD0` |
| `api-docs/api-status.md` | `6F4887AEE8C3DAEC098B576DE7216C082D53AF61DECEFC7E655AD20AEDE450A2` |
| `api-docs/api-supplier-profile.md` | `1C298019839C9BBCC9880DCD26281690FD93B4F4D9E1795BC3B886322D27D132` |
| `api-docs/2026-08-12-channel-update-restore-probe.md` | `050D62D6F1AFA2A2A988A130DDC7D052B9D08D2CD82692816154E9DE275EE612` |

## 5. 冻结验证

```text
Canonical markers: 61
Canonical reverse diffs: 0
Overweight model residue: 0
Subsystem triad requirement closure: 17/17
Markdown fence validation: passed
Local link validation: passed
Duplicate test ID validation: passed
```
