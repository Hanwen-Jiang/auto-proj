# Sub2API v0.1.172 原生 A6 Listing 经营模块设计档案

> 历史设计版本：2026-08-11 v9。
> 当前可执行 Prompt 是 `docs/sub2api-a6-channel-implementation-prompt.md`；本文件保留演进细节，其中旧的
> recovery consumer、PROBE_ONLY和canary恢复流程已被当前Prompt废止，不作为编码依据。

请在当前定制版 Sub2API v0.1.172 中实现原生 A6 Listing 经营模块。主体逻辑从
`/Users/haven/Documents/code/a6api-auto` 的 Trader 迁移并重新梳理，保留现有
Managed Seller、Billing Adjustment、A6 Provider、Pin、Auto-Pool、Replay 及生产升级兼容能力。

本模块必须以“输入/上游部门”和“输出/下游部门”为一级边界。任何表、字段、Key、状态、Saga、
控制器、审计日志和管理端操作都必须明确归属部门，不得继续使用含糊的 `token`、`key` 或
`paused` 表达两类不同资源。

## 0. 编码前交付物

实际编码前先提交并等待审核：

1. 完整数据模型、唯一约束、外键、Decimal 精度和敏感字段加密方案。
2. 输入部门、输出部门及连接层的边界图。
3. Listing 正交状态模型、Class1 路由状态机、两个独立 Key 轮换 Saga。
4. 每类远端写入的写前读、写入、结果不明、精确读回、补偿和人工重放顺序。
5. 现有 Managed Seller、A6 Provider、OAuth、API Key 的逐行迁移映射。
6. 对 `docs/a6-api-reference.md` 的差异清单，包括尚缺的端点、请求字段、响应字段和状态字段。
7. 触发事件、扫描频率、合并窗口、批次大小、幂等键、租约和失败出口。

## 1. 两部门与连接层

### 1.1 输入/上游部门

输入部门负责 Sub2API 向上游取得服务能力：

- Class1 A6 Provider、Class2 原生 OAuth、Class3 上游 API Key。
- `provider_route_token`、Auto-Pool、Pin、Provider liquidity、owned-Channel Ban。
- 账号成本 `sk`、profit、模型能力、健康、并发和 Sub2API 原生调度状态。
- 上游路由 generation、成本 generation、模型能力 generation 和账单 generation。

### 1.2 输出/下游部门

输出部门负责向 A6 Listing 输出服务：

- `a6-channel` 用户、唯一 `output_channel_ingress_key`、ToA6API 分组。
- 一个本地输出用户对应一个远端 A6 自有 Channel；一个 Channel 下包含多个按模型区分的 Listing。
- 每个远端 Listing 独立保存 `ck`、价格边界、价格分布、上下架、cooling 和收益核对。
- Channel 绑定、Listing 绑定、模型入口解析、下游计费和远端状态 readback。

### 1.3 连接层

连接层先由 `(local_user_id, requested_model)` 唯一解析稳定的本地 Listing binding，再以
`(listing_binding_id, account_id, normalized_model)` 计算 eligibility 和 delta，把输入部门的可用账号连接到该
Listing。请求 attempt 必须同时固定：

```text
output_channel_generation
listing_generation
account_generation
route_generation
cost_generation
capability_generation
billing_generation
delta_effective
```

上述 generation 在真正发出上游请求前任意一项失效，该 attempt 必须重新经过同一硬门。上游请求已经发出后，
该 attempt 持有其 generation 引用并按派发时的不可变经济快照完成；generation 切换只阻止新 attempt，不得用
切换后的 ck、sk 或 delta 回写已经执行的 attempt。只有凭据泄漏、人工强制终止等明确 hard revoke 才中止在途请求。

## 2. A6 Typed Client 与控制面

1. 统一使用 `(a6_user_id, system_access_token)` 调用 A6 控制面。这里的 `a6_user_id` 是远端 A6 用户 ID，
   不是 Sub2API 本地 `users.id`。
2. 一套 A6 凭据代表一个远端 A6 用户，可绑定多个本地输出 Channel 用户、多个 Listing 和多个输入部门资源。
3. Typed Client 完整覆盖
   `/Users/haven/Documents/code/a6api-auto/docs/a6-api-reference.md`。已确认的 Token 删除接口为
   `DELETE /api/token/{token_id}`；还必须补齐本设计直接依赖但尚未封装的 `/api/platform-models` 和
   `GET /api/marketplace/circuit-states`。
4. `system_access_token` 加密存储；日志、错误、trace 和审计摘要全部脱敏。
5. 每个写操作保存操作者、资源归属部门、请求摘要、响应摘要、source generation、幂等键、写前读、
   写后精确读回和最终 disposition。
6. POST/PUT/PATCH 的传输失败不得盲目重试。先进入结果不明状态，再用对应 GET 精确读回决定 adopt、replay
   或补偿。
7. 管理端覆盖主要工作流，并提供管理员专属通用 A6 API 调用控制台。控制台只能调用 Typed Client
   白名单端点，不接受任意 URL。GET 可直接执行；命中已受管 Channel/Listing/Token 的写请求必须转换成相同的
   desired generation + Outbox operation并由对应单写者执行，控制台不得旁路 Reconciler。未受管资源写入也必须
   获取资源锁、审计、精确读回，并触发 inventory drift scan。
8. 所有集合读取必须 exhaust分页并保存 `page/size/total/item_count/snapshot_hash/completeness`。响应达到服务端
   limit但没有下一页协议时标记 snapshot incomplete；不完整快照中“没有某资源”不能用于清除 hold、确认 Ban、
   删除绑定或恢复准入。`circuit-states` 固定请求 `limit=1000`；返回恰好 1000 项时产生容量告警并等待管理员提高
   上限，不用该快照中的“未出现”证明某 circuit 已关闭。
9. `system_access_token`、Provider远端 Key和为远端 Channel重放所需的 ingress Key材料使用
   envelope encryption与独立 secret scope；数据库业务表只保存 secret reference、fingerprint和 last4。本地 API
   Key鉴权仍使用 Sub2现有不可逆 hash。若旧 ingress Key没有可恢复 escrow，远端 Channel替换时必须走轮换 Saga，
   不能从 hash推导或把日志中的 Key当真源。

## 3. 输出部门：一 Channel 一用户一 Key、多 Listing

### 3.1 关系与约束

新增 `users.user_type='a6-channel'`，它的身份颗粒度是远端 A6 Channel，而不是单个 Listing：

```text
users(user_type='a6-channel')
    1:1 a6_output_channels
    1:N output_channel_ingress_key versions，任意时刻 <=1 有效
    1:N a6_listing_bindings
```

本地 binding 是稳定业务身份，A6 Channel ID 和 Listing ID 是可能因删除、重建或替换而变化的远端实例身份。
最低约束：

```text
a6_output_channels.local_user_id UNIQUE
a6_output_channel_keys.api_key_id UNIQUE
UNIQUE active key per output_channel_id (partial unique index)
UNIQUE (a6_output_channel_id, normalized_model)
UNIQUE current channel instance per output_channel_id (partial unique index)
UNIQUE current a6_channel_id across channel instances (partial unique index)
UNIQUE current listing instance per listing_binding_id (partial unique index)
UNIQUE current a6_listing_id across listing instances (partial unique index)
```

`a6_output_channels` 至少保存：

```text
id, local_user_id, a6_credential_id, current_channel_instance_id,
desired_channel_generation, observed_channel_generation,
channel_lifecycle_state, remote_channel_state, last_exact_readback
```

`a6_output_channel_instances` 至少保存：

```text
id, output_channel_id, a6_channel_id, a6_supplier_channel_binding_id,
remote_owner_id, valid_from, valid_to, replacement_reason,
raw_channel_snapshot, last_exact_readback
```

`a6_listing_bindings` 至少保存：

```text
id, a6_output_channel_id, normalized_model, normalization_version,
current_listing_instance_id, binding_lifecycle_state,
listing_generation, pricing_generation, state_generation,
last_confirmed_ck, desired_remote_state, observed_remote_state,
observed_callable, last_exact_readback
```

`a6_listing_instances` 至少保存：

```text
id, listing_binding_id, channel_instance_id,
a6_listing_id, a6_channel_id, own_model, platform_model,
remote_owner_id, valid_from, valid_to, replacement_reason,
raw_listing_snapshot, last_exact_readback
```

`a6_listing_id` 是远端不透明实例主键；`(a6_channel_id, model)` 只用于交叉校验。当前 Listing instance 的
Channel 和 owner 必须与父 Channel 当前 instance 一致。一个 Channel 下同一规范化模型只允许一个有效 binding。
远端 Listing 删除后为同一 Channel/模型重建时，关闭旧 instance、创建新 instance并更新
`current_listing_instance_id`，不得新建第二个业务 binding，也不得让历史 override、attempt、hold 或账单改指新
远端 ID。

用户初始余额为 0，允许持续负余额；余额只作为输出经营应收账本，不参与请求准入。当前源码已确认
`concurrency=0` 表示无限并发，`api_keys.quota=0` 表示无限配额。每个输出 Channel 用户只有一个 quota=0、
无到期时间的有效入口 Key。

禁止该用户普通登录、自助注册和用户端 Key 管理。所有 `a6-channel` 用户只能使用全站唯一专属组
`ToA6API`；普通用户严格排除该组。该组禁止改名、删除、复制授权和误授权，默认倍率和所有
`user_group_rate_multipliers` 的有效结果必须锁定为 `1.0`。旧的经济倍率全部迁移到 Listing/账号经济快照，
不得再由分组倍率表达。

ToA6API 的输入账号成员由 Class1/Class2/Class3 binding显式投影；没有 class、成本合同或当前模型能力 generation
的普通账号不得因“属于默认分组”而进入候选。账号能否同时服务其他普通分组沿用 Sub2现有管理员配置，但任何
a6-channel请求最终都必须经过本模块按 `(listing_binding, account, model)` 的额外硬门。

### 3.2 请求入口与模型解析

现有 Sub2API API Key 鉴权会把一个 Key 固定绑定到一个 `UserID`，因此不得实现“同一 Key 根据模型切换多个
本地用户”。标准请求顺序为：

```text
Authorization API Key
-> 固定 local_user_id
-> 读取唯一 a6_output_channel
-> 在账号调度前规范化 requested model
-> 按 (a6_output_channel_id, normalized_model) 唯一解析 a6_listing_binding
-> 读取该 Listing 当前 eligibility projection
-> 选择 Sub2 account
-> 获取账号 slot
-> 重新核对 account health + 全部 projection generation + 经济硬门
-> 持久化 attempt 经济快照并 CAS为 dispatched
-> 发出上游请求
```

没有唯一 Listing、模型不属于该 Channel、绑定 generation 过期或父 Channel 不一致时，按用户请求错误短路，
不选择账号、不改变账号健康、不触发 Auto-Pool 扩池。不得用 upstream response model、模糊家族匹配或默认
Listing 替代入口 Listing 身份。

每条 usage log、attempt ledger 和 billing ledger 都必须同时带：

```text
local_user_id, output_channel_id, ingress_api_key_id,
listing_binding_id, listing_instance_id,
a6_listing_id_snapshot, a6_channel_id_snapshot,
requested_model, normalized_model,
winner_account_id, attempt_id, economic_snapshot_id
```

余额是 Channel 用户级汇总，但价格、delta、资格、上下架和收益核对始终按 Listing/模型拆分。Channel、owner、
入口 Key 或远端 Channel 级故障必须以同一 source generation 扇出到其全部 Listing；模型级故障只影响对应 Listing。

模型规范化必须以 A6 `/api/platform-models` 的精确 `id/model_no` 和版本化显式 alias 表为准：

1. 保存 `normalization_version`，禁止按前缀、大小模型家族或响应中的相似字符串做模糊匹配。
2. requested model 只能精确命中 `model_no` 或一条显式 alias；没有命中时在 scheduler 前短路。
3. 同一 output Channel 内两个远端 Listing 规范化到同一模型时，二者都进入 `BINDING_REVIEW`，不得按价格、创建
   时间或 Listing ID 自动选一个。
4. alias 或 platform-model 目录升级先生成候选 normalization generation，完成碰撞审计后再原子切换。

`users.status` 只表达整个输出 Channel 的鉴权生命周期，不表达子 Listing 状态。只要 Channel 未退役、唯一入口
Key 有效且没有管理员 Channel 级 hard hold，`users.status` 保持 active；单个乃至全部 Listing 因 NO_ELIGIBLE、
本地 cooling、远端下架或远端 cooling 停止服务时，均由 requested-model Listing admission gate 拒绝，不能把共享
用户设为 inactive。`/v1/models` 对该 Key 只返回当前 output Channel 已绑定、未退役且允许暴露的规范化模型，
绝不返回其他 Channel 的模型；瞬时 cooldown 不修改模型目录，实际请求仍以 Listing admission 为准。

### 3.3 `output_channel_ingress_key` 独立资源

`output_channel_ingress_key` 属于输出部门，是一个 A6 自有 Channel 调用 Sub2API 的本地入口凭据。它与
Class1 的远端 `provider_route_token` 使用不同表、字段、服务、权限和轮换 Saga，禁止复用含糊的 `token_id`
或 `key_id` 字段同时指向两者。

Channel 入口 Key 轮换必须保证任意时刻最多一个本地 Key 可用：

```text
锁定 output_channel_id
-> 创建 DISABLED/STAGED 新 Key
-> 给该 Channel 下全部 Listing 增加 OUTPUT_KEY_ROTATION hold
-> 全部远端 Listing 下架并逐项读回 UNLISTED
-> PATCH 远端 A6 Supplier Channel 使用新 Key
-> PATCH 结果不明时保持全部 Listing 下架，并先做 Channel 精确 GET/readiness 判定
-> 本地事务中禁用旧 Key、启用新 Key
-> 按批次 resubmit 全部 Listing
-> 逐项确认 Supplier 已上架、Marketplace 精确可见且对应 circuit 未冷却
-> 清除 hold 并开放对应 Listing
-> 删除旧本地 Key并读回不存在
```

远端不回显完整 Key 时，readback 必须结合可保存的 Key fingerprint、Channel 配置版本、Supplier 状态、
Marketplace 精确项和 circuit 状态。
结果不明时可以在资源锁内让旧、新 Key 依次单独成为唯一有效 Key进行判定，但任何时刻不得同时启用两个 Key。
失败时保持该 Channel 下全部 Listing 下架，并根据已确认的远端 Channel 配置选择继续、回滚或人工重放。

### 3.4 已确认的 Sub2API 代码事实与负余额改造

当前定制版源码根目录为
`/Users/haven/Documents/code/codex-pool/sub2api-update-20260808-v0172`。以下事实已经确认，编码阶段不得重新按
猜测设计：

- `backend/internal/server/middleware/api_key_auth.go` 把 Key 绑定的 `apiKey.User.ID` 写入请求 `UserID`。
- `backend/internal/service/api_key.go::IsQuotaExhausted` 明确 `quota<=0` 为无限配额。
- `backend/internal/service/concurrency_service.go::AcquireUserSlot` 明确 `maxConcurrency<=0` 为无限并发。
- `backend/internal/repository/usage_billing_repo.go::deductUsageBillingBalance` 已有余额不足后继续原子扣成负数的分支。
- API Key middleware 和 `BillingCacheService.CheckBillingEligibility` 当前仍拒绝非正余额。
- `backend/migrations/194a_a6_custom_compat.sql` 当前仍把 Managed Seller 余额和触发器固定为
  `999999999999.00000000`。

实现时必须：

1. 给 Ent schema、service DTO、repository scan、auth cache snapshot 和管理 API 全链路增加真实 `user_type`。
2. 删除 Managed Seller 巨额余额 CHECK、回填和强制重置触发器；迁移后的输出 Channel 用户余额初始化/切换为 0。
3. 仅对 `user_type='a6-channel'` 跳过 API Key middleware 与 BillingCache 的余额、minimum reserve 和
   user-platform quota 准入；用户/Key 状态、IP、唯一分组、模型绑定、RPM、并发和 eligibility 仍照常执行。
4. 保留 `usageBillingRepository.Apply -> deductUsageBillingBalance`，每个 winner 正常扣费并允许余额持续下降为负数。
5. 缓存必须接受、发布和失效负余额，不得把负数钳制为 0，也不得因为负数把 Key 状态改为 quota exhausted。

### 3.5 Channel/Listing 创建、替换与退役

新建或采用一个输出 Channel 必须使用同一 Saga：

```text
创建 status=inactive、balance=0 的 a6-channel 本地用户
-> 创建 DISABLED/STAGED output_channel_ingress_key
-> 创建 a6_output_channels；新增远端 Channel 时调用 POST /api/supplier/apply
-> 已有 Supplier 同样用该端点申请第二个 Channel，轮询 requests 直到受理、channels 出现新 ID
-> 创建远端 Channel instance
-> 将唯一入口 Key 写入远端 Supplier Channel
-> 以 last4 + Channel 配置精确确认
-> 同步 Channel 下完整 Listing inventory
-> 为每个模型建立稳定 listing binding 和当前远端 instance
-> 生成价格、模型能力和 eligibility generation
-> 对所有受影响 Class1 Token/模型刷新 owned-Channel Ban attestation
-> 远端 Listing 保持下架，直到对应 binding 满足开放条件
-> 启用唯一入口 Key和本地用户
-> 按第 9、10 节逐项 resubmit，确认 Marketplace 精确可见、circuit 未冷却后开放
```

新 owned Channel 出现后，旧 Ban attestation 立即失效。先读完整 marketplace；已禁用则只更新 attestation，存在
缺口时只摘受影响的 Class1 Token/模型，完成 disable/readback 后恢复，不能让仍可能自路由的 Class1 继续派发。

生命周期规则：

1. 远端 Listing 被删除但父 Channel 和 canonical model 不变：保留 `listing_binding_id`，关闭旧 instance，创建新
   Listing/readback 后挂为 current instance；历史账本保持指向原 binding 和当时的远端 instance snapshot。
2. `platform_model` 真正改变：退役旧 binding并新建 binding，不原地改写历史模型。只有 display/own_model 变化且
   canonical platform model 未变时，才允许生成新的 binding generation。
3. 远端 Channel 删除或必须替换：给全部子 Listing加 `OUTPUT_CONFIG_INVALID` hold，通过
   `POST /api/supplier/apply`申请新Channel，获批后创建新的Channel instance，重新写入同一个output ingress Key，
   逐项重建/采用Listing instance，完成Ban和精确读回后再清hold。稳定的
   `a6_output_channels.id`、`users.id`、Key 与历史账本保留。
4. owner 冲突、多个候选替代实例或无法唯一确认父子关系时进入 `BINDING_REVIEW`；管理员只能选择“绑定指定远端
   实例”“接受远端事实并生成新 generation”或“退役”，不能直接修改 observed 字段。
5. 退役一个 Listing：先关闭本地准入并等待该 binding inflight=0；超时则保持退役 pending，不提前破坏远端价格/
   Key。清零后远端 takedown/readback，再关闭 current instance、标记 binding retired并移除当前 projection；保留
   attempt、usage、revenue 和审计历史。
6. 最后一个 Listing 退役：全部 binding完成上述 drain 后，远端 Channel takedown/readback，禁用并删除本地入口 Key，关闭 Channel instance，
   将本地用户设为 inactive/retired。空 Channel 不保留一个可调用但没有模型绑定的入口 Key。

## 4. 输入部门：账号分类与 `provider_route_token`

### 4.1 Class1：A6 Provider 回代

Class1 是输入部门的 A6 Provider API Key 账号。`provider_route_token` 是远端 A6 请求面 Token，使用
一对一绑定，至少保存：

```text
account_id UNIQUE, a6_credential_id, remote_token_id UNIQUE, model,
route_mode=auto_pool|pin, desired_route_generation, observed_route_generation,
route_state, subscription_id, pin_channel_id, token_state, last_exact_readback
```

Provider Token 轮换 Saga：

```text
创建新远端 Token
-> 从旧 Token 读取并恢复完整配置
   （模型限制、额度、Auto-Pool 区间/订阅或 Pin Channel）
-> 对新 Token 执行 owned-Channel Ban 预检/修复和精确读回
-> 验证新 Token 的 route、cost、model、callability
-> 原子切换本地 account -> remote_token_id 绑定
-> DELETE /api/token/{old_token_id}
-> GET detail 返回 record not found 且 Token 列表中不存在旧 ID
```

创建的 A6 Token名称从 `token_name_corpus.json` 随机选择，Provider/recovery/liquidity duty只存控制面，不编码进名称。
创建接口若返回不带 `sk-` 的 key material，持久化和数据面使用前规范化为单个 `sk-` 前缀，禁止重复添加。

新 Token 就绪前旧 Token 继续服务，但只有当前绑定的 Token 可被本地调度。Token DELETE 已于 2026-08-11
使用 `(a6_user_id, system_access_token)` 和临时 Token 实测成功；HTTP 200 不作为完成证据，必须同时检查
`success=true` 和上述详情/列表双读回。

### 4.2 Class2：原生 OAuth

1. 细分 Free、Plus、Team、K12、Pro，以人工管理为主。
2. 每个账号保存 `account_cost_cny` 和 `expected_weekly_quota_multiplier`。
3. `expected_weekly_quota_multiplier` 默认 `1.0`；数据库只约束其 `> 0`，UI 输入边界由管理员参数配置。
   18%-400% 只是账号可能值示例，不是数据库硬范围。
4. utilization 在数据库统一保存为 Decimal 比例，`6%` 保存为 `0.06`，允许大于 `1.0`，禁止负数。它只用于
   估算 `sk`，不改变调度、健康、生命周期或账号状态：

```text
utilization > 0:
    one_week_A = used_A / utilization
utilization = 0:
    one_week_A = 100
expected_total_A = one_week_A * expected_weekly_quota_multiplier
sk = account_cost_cny / expected_total_A
```

只有 `used_A>0 AND utilization>0` 才使用除法。二者不一致、字段缺失或分母为 0 时，保留最近确认的 sk；没有历史
值时使用 `one_week_A=100` 的临时估算并标记 `CLASS2_COST_ESTIMATED`，不得产生 NaN、Infinity 或改变账号状态。
`utilization=100%` 仍按上述公式计算，账号继续完全遵循 Sub2API 原有 OAuth 调度规则。不得新增 EXHAUSTED、
自动周刷新、quota epoch、自动替换或自动退役逻辑。

### 4.3 Class3：上游 API Key

1. 使用稳定类别 `class3:<provider_code>`。首期管理员填写账号级 `declared_sk_default`，并可按规范化模型填写
   `declared_sk_override`；最终 sk 事实始终按 `(account_id, normalized_model)` 版本化，模型 override 优先于默认值。
2. 后续站点适配器同步上游模型、余额、Acost 和账单偏差。
3. 15 分钟账单窗口至少包含 20 个收费请求且 expected cost >= CNY 1：偏差 <=5% 正常；5%-15% 告警；
   >15% 连续 3 个窗口暂停；单窗 >30% 立即暂停；无 Acost 时只标记未核验。
4. 暂停后的恢复证据来自：重新同步上游账单和余额 -> 有界审计 Probe -> 连续 3 次偏差 <=5%
   -> 新 billing generation -> 恢复。审计 Probe 不依赖生产流量。
5. 审计 control path只可绕过“由 billing deviation controller自己设置的暂停”，仍必须满足凭据、模型、原生健康和
   Probe预算；账号若因 auth、quota或 Sub2原生故障暂停，不得借审计 Probe强行调用。

### 4.4 Profit 与模型能力

- Class1 默认 profit=0.20，Class2 默认 0.50，Class3 默认 0.40。
- 默认值按类别配置，允许账号级覆盖；每个 `class3:<provider_code>` 有独立默认项，初始回落到 0.40，后续修改
  `class3:vovoapi` 不得影响其他 provider_code。
- Class2/Class3 入池前同步 `/v1/models`，并对当前 A6 OpenAI Listing 模型执行有界探测，生成版本化模型白名单。
- 探测失败的模型不进入该账号候选集；账号其他模型和生命周期仍遵循 Sub2API 原规则。

## 5. Class1 Auto-Pool、Ban 与路由 generation

### 5.1 成本

Class1 按 route mode生成 sk：

```text
auto_pool:
    sk = topN_max_cny_ratio of the complete included price band

pin:
    sk = exact pinned Channel landed K_CNY
    source epoch = pin_assignment_epoch + marketplace snapshot generation
```

Pin cost必须由 `pin_channel_id/model` 精确 marketplace readback取得，Channel不可调用、价格缺失或 assignment epoch
不匹配时 Pin Class1不可调度。两种模式都不得再叠加 `realized_cost_p95`、`retry_reserve` 或重复的 `5/6` 换算。
`topN_mean`、variance、min
仅用于观测，不参与硬门。

默认参数：

```text
target_n=10, min_n=3, max_n=20,
min_ratio=0, hard_max_ratio=0.3
```

第 N 个 Channel 所在的同价带整体纳入。每 60 秒刷新 fresh marketplace snapshot；成本上涨立即生成新
cost generation，成本下降连续 3 个快照确认后生效。

例行 marketplace刷新暂时失败时保留最近确认的 route/cost generation并标记 readback stale，不因 freshness TTL
自动摘流或把 sk置空。若是运行错误触发扩池，则必须取得 fresh完整 snapshot才能激活新 generation；读取失败时
受影响 Class1保持暂停，其他使用各自有效 generation 的账号继续服务。

### 5.2 Owned-Channel Ban：先读后写

每次 Token onboarding、首次启用 Auto-Pool、路由区间变更、Pin 变更和恢复前，先完整分页读取：

```text
route_status=all
exclude_unavailable=false
include_seed=true
include_disabled=true
include_user_disabled=true
```

合并远端 active/down/unavailable/hidden/retired inventory 与本地全部 controlled Listing Channel，并按当前
`provider_route_token + normalized_model` 形成完整 owned `(channel_id, model)` 集合。

- 若全部 owned Channel 已满足 `user_channel_disabled=true` 且 `route_status=user_disabled`，记录带
  snapshot hash 的 Ban attestation，跳过写入。
- 只对缺失或漂移 Channel 执行 disable，并逐项精确读回。
- 若某 owned Listing 只有在上架后才出现在 Provider marketplace，进入 maintenance-listing 流程：先把可能选中
  它的受影响 Class1 Token/模型设为不可调度，保持输出 Listing `admission=CLOSED`，临时上架该 owned Listing
  -> 等待 marketplace 可见 -> 执行 Ban/readback -> 立即恢复原远端状态 -> 再读 Auto-Pool 无 owned交集
  -> 恢复受影响 Class1。
- maintenance-listing 只处理缺失项，只摘可能自路由到该 owned Channel 的 Class1 Token/模型；其他已经有有效 Ban
  attestation 的 Class1 继续服务。绝不在 Provider 仍可调度时先临时上架再 Ban。
- Auto-Pool 写后必须确认 `auto_channels` 非空且与 owned set 无交集，否则该 Class1 不可调度。

### 5.3 Auto-Pool 扩缩

Auto-Pool route generation 的任一上游失败无需取得实际命中 `channel_id`。当前区间视为整体失效或已集体改价：

```text
暂停受影响 Class1
-> fresh marketplace snapshot
-> target_n + 1
-> 选择下一完整价格带
-> 新 route/cost generation
-> Ban attestation
-> PUT Auto-Pool
-> 精确读回
-> 激活新 generation并恢复该 Class1
```

稳定至少 30 分钟、成功至少 100 次、无上游归因错误、距上次变更至少 30 分钟，且
`cv_n>5%` 或 `tail_jump>3%` 时，每轮 `target_n-1`。达到 `max_n` 后出现新失败则冷却该 Class1；恢复后从
`max_n` 开始，稳定后逐级缩小。fresh snapshot 在 `hard_max_ratio=0.3` 内找不到任何非 owned可调用 Channel，或
完整下一价格带会越过 0.3 时，该 Class1保持不可调度并进入可见的 `AUTO_POOL_RANGE_EXHAUSTED`，不得截断同价带、
使用空 `auto_channels` 或突破硬上限。Pin 模式不使用 Top-N。

Pin mode的运行失败按现有去重阈值绑定 `pin_assignment_epoch`；达到确认阈值后暂停该 Class1，生成新的 repin
generation，完成 owned-set校验、Pin写入、Pins/marketplace/cost精确读回后恢复。它不得执行 `target_n+1`，也不得
自动切换为 Auto-Pool；写结果不明同样先读回当前 Pin再决定 adopt或重放。

### 5.4 PUT 结果不明

旧 route generation 只在远端 PUT 发出前确定有效。PUT 超时、连接断开或响应丢失后：

```text
仅摘受影响 Class1
-> route_state=ROUTE_UNKNOWN
-> 不再声称旧 generation 正在远端执行
-> GET 订阅/Pin/Token 精确读回
```

- 读回等于新 desired：adopt 新 observed generation。
- 读回仍等于旧配置：以同一 operation identity 重放 desired 写入。
- 读回为第三种配置：保持 ROUTE_UNKNOWN，基于 observed reality 重新规划或由管理员处理。

Provider shadow 日常保持 disabled/inactive。仅当确需新的不可变 generation 且没有可复用的 active/planned
generation 时允许一次性运行，并记录具体原因；完成后仍保持 shadow timer disabled。

## 6. Listing 价格坐标、上下限和市场分布

### 6.1 `ck` 与汇率

`commission=0.2` 为全局固定值。`ck` 是商户实际净 `K_CNY`，公开倍率为：

```text
ck_public = ck * (1 + commission) = ck * 1.2
```

A6 markup 必须进入输出配置读回。批量调用 Supplier Channel markup preview，使用逗号分隔的 `channel_ids`，将
`data.items[]` 按 `channel_id` 建立映射并保存 `percent/source`；响应顺序不得作为绑定依据。再用收益明细中的
`markup_percent`、`user_market_quota/supplier_earn_quota` 交叉验证。当前实测应满足：

```text
markup_percent = 20
markup_preview.percent = 20
markup_preview.source = "supplier"
user_market_quota / supplier_earn_quota ~= 1.2
earned_micros = supplier_earn_quota
```

若当前 Channel/Listing 的远端 markup 与固定 `commission=0.2` 不一致，输出配置控制器 upsert
`OUTPUT_CONFIG_INVALID`，停止生成新 delta 和新价格写入，保留最近确认的 ck/commission/收益事实用于历史结算，
并按正常 hold 流程下架。markup 恢复为 20%、价格向量和 earnings canary 均读回一致后，才能清除此 hold。
控制面重新确认 20% 后允许生成一个不对生产发布的 `canary-only` pricing/delta generation，用当前精确价格向量
执行同一 eligibility硬门并只供 PROBE_ONLY；它不能绕过 `delta<=0.5` 或利润公式。没有合格账号时同时保留
NO_ELIGIBLE，等待正常候选恢复，不借审计流量强行开通。

`official_*_price_micros` 来自 A6 `/api/platform-models?enabled=1&size=1000`。canonical `ck` 按 billing 类型选择
维度，不能固定除以 input 官方价：

```text
fx_usd_cny = realtime_ratio_cny / realtime_ratio_usd

if billing == per_token:
    canonical_dimension = first dimension in
        [input, output, cache_read, cache_write]
        whose official_price_micros > 0
elif billing == per_request:
    canonical_dimension = per_request
    require official_per_request_price_micros > 0
else:
    OUTPUT_CONFIG_INVALID

ck = fx_usd_cny
     * listing_price_micros[canonical_dimension]
     / official_price_micros[canonical_dimension]
```

不存在非零官方维度、canonical Listing 价格为 0 但其他维度非零、或远端 charge type 与平台 billing 不匹配时，
不得除零或切换到一个看起来可用的维度，直接进入 `OUTPUT_CONFIG_INVALID`。写目标 `ck` 时，对每个非零官方维度：

```text
listing_price_micros[d] = ceil(official_price_micros[d] * ck / fx_usd_cny)
```

官方价格为 0 的维度写 0。写后用同一 canonical 规则从远端整数值重新计算 `last_confirmed_ck`；其他所有非零维度
分别计算 implied ck，并在整数向上量化容差内交叉校验。

A6 官方目录必须落为版本化 `a6_model_price_generations`，保存 platform model id/model_no、billing、完整 USD
micros向量、official_source、raw hash和 observed_at。ToA6API 的 `base_actual_cost` resolver必须引用 attempt中冻结的
同一 A6 model price generation，不能继续读取一份独立的旧 Sub2模型价。对 `model_no` 可精确一一映射的 Sub2
模型目录，使用独立事务和 cache invalidation同步同一官方向量；alias碰撞或 Sub2特有模型保持原价并进入映射审核，
不得模糊覆盖。官方向量变化生成新 generation并立即重算价格、sk相关换算和 eligibility，新请求切新版本，旧
inflight继续使用快照版本。

FX 主来源是 A6 marketplace 同一有效快照中的 `realtime_ratio_cny/realtime_ratio_usd`。兜底合同固定为：

```text
fallback_provider = frankfurter
fallback_url = https://api.frankfurter.app/latest?from=USD&to=CNY
json_path = rates.CNY
request_timeout = 3s
cache_ttl = 30m
refresh_interval_while_fallback_active = 15m
max_source_date_age = 96h
credential_secret_ref = null
```

`provider` 表示具体请求哪个外部服务及如何解析响应；`cache_ttl` 表示一次成功报价在多长时间内直接复用，
避免每个请求访问外站；`credential_secret_ref` 表示外部 provider 若需要 API Key 时只保存 secret 引用。
Frankfurter 当前不需要凭据，所以该值固定为 null，不新建用户名、密码或 Token。

外部源只在 A6 报价缺失、字段非法、超时或不可达时启用，不与 A6 做双源投票或仲裁。A6 恢复后下一次刷新
立即切回 A6。保存 `source/base/quote/source_date/fetched_at/raw_response_sha256`；周末允许 source date 最多落后
96 小时。A6 和 Frankfurter 都不可用时只读取 `last_confirmed_fx` 维持现有 ck/eligibility，不发起新的价格写入，
也不生成虚构汇率。

Supplier Listing 写入/读回的四项 micros、Marketplace 公开倍率、A6 官方价格向量、FX source/version 分别保存。
四项比例不一致时重新读取 A6 platform model 和 Supplier Listing，以 A6 返回结果为准；canonical ck 始终由
上述 billing/canonical dimension 推导，其他维度记录 drift 并按 A6 当前向量修复。A6 连续规范化为不同值时采用
其精确 readback，不建立额外停用/重新启用状态。

所有金额和倍率使用 NUMERIC/Decimal；最终判定不使用 float。

### 6.2 管理员硬上下限

```text
listing_hard_min_ck default 0.025
listing_hard_max_ck default 4.0
constraint: 0 < listing_hard_min_ck <= listing_hard_max_ck
```

两者是全局默认值，实际保存到版本化的逐模型 pricing policy，均可由管理员修改。默认上限表示任何我方 Listing
的 ck 不得超过 `4.0 K_CNY`，不是“超出后自动下架”。管理员写入和自动分布目标都必须先裁剪到当前模型 policy。

对每个模型：

```text
effective_delta_cap = 0.5 if delta_transport_capable else 0
class1_economic_floor = min_ready_class1_for_model(
    sk * (1 + profit) / (1 + effective_delta_cap)
)

raw_lower = max(listing_hard_min_ck, class1_economic_floor)
band_lower = min(raw_lower, listing_hard_max_ck)
band_upper = listing_hard_max_ck
```

`ready_class1_for_model` 必须同时满足 Sub2原生可调度、模型白名单、当前 route/cost generation有效和 owned Ban
attestation有效；瞬时满并发不改变 ready 事实。没有 ready Class1 时，经济下界不参与，
`band_lower=listing_hard_min_ck`。当 `raw_lower>hard_max` 时价格区间收缩为
`[hard_max, hard_max]`，不得产生超过 hard max 的价格；账号资格仍由 eligibility 逐个判断，只有候选集最终为空
才触发 Listing 下架。

### 6.3 按 A6 原市场频率分布

1. 对 `[band_lower, band_upper]` 使用 8 个对数桶。边界为
   `edge_j = lower * (upper/lower)^(j/8)`，最后一个桶包含 upper；上下界相等时退化为单点价。
2. 样本只取 A6 外部、可调用、非 owned Channel。Marketplace 返回用户侧公开倍率时，先按
   `sample_ck = sample_public_k / (1 + confirmed_commission)` 换算到商户净坐标，再仅使用落在当前
   `[band_lower, band_upper]` 内的样本；禁止把公开 k 和商户 ck 混在同一组桶中。
3. 使用最大余数法把我方 Listing 数量分配到 8 个桶；桶内目标价取外部样本的实际分位点，而不是桶中心。
4. 零样本模型保持每个 Listing 当前已确认价格，不猜目标分布；新 Listing 尚无 confirmed价格时以
   `band_lower` 作为确定性初始目标，写后读回并重新跑 eligibility，候选仍为空则保持下架。
5. 使用稳定匹配最小化 `abs(log(current_ck/target_ck))`；等成本时保持原 Listing 顺序，避免无意义交换。
6. `pricing_write_batch_size` 默认 10，可配置；普通分布调价每个 Listing 至少间隔 5 分钟。
7. 越过管理员硬界、当前 ck 不合法或经济事实上涨导致 eligibility 风险时，可绕过普通 5 分钟间隔立即纠正。

## 7. Delta、计费与经济硬门

### 7.1 `(Listing, Sub2 account)` 颗粒度

对每个稳定的 `(listing_binding_id, account_id, normalized_model)`：

```text
required = sk * (1 + profit)
delta_auto = max(0, required / ck - 1)
delta_effective = COALESCE(delta_override, delta_auto)

eligible =
    model_capable
    AND account_schedulable_by_sub2api
    AND delta_transport_capable
    AND 0 <= delta_effective <= 0.5
    AND ck * (1 + delta_effective) >= required
```

公式中的 `ck` 是当前可派发 pricing generation 的 `dispatch_ck`，通常等于远端 `last_confirmed_ck`；价格下调 Saga
期间使用第 7.6 节更保守的 `min(last_confirmed_ck, desired_ck)`。不得用尚未写后读确认的 desired ck提高资格。

经济门中不得再次乘 commission，因为 ck 已是商户净收入坐标。`delta_override` 可空，范围 `[0,0.5]`，永久
有效且无需审批；清空即恢复 `delta_auto`。override 使用稀疏表：

```text
a6_listing_account_delta_overrides:
    listing_binding_id, account_id, normalized_model, delta_override,
    updated_by, updated_at
PRIMARY KEY (listing_binding_id, account_id, normalized_model)
```

`delta_auto` 是派生事实，不作为每对关系的人工主数据。持久化 ck/sk/profit/模型能力的版本化事实，增量生成
eligibility projection；Redis 只投影当前 eligible account IDs、delta 和 generation。override 低于所需 delta 时，
最终公式仍使该账号不合格，不得把 override 当成绕过硬门的许可。

Redis projection 必须携带 listing/account/cost/capability/billing generation。请求命中缺失或过期 projection 时，
只允许同步重建并再次核对 generation，重建超时则走普通服务暂时不可用；不得回退使用旧 eligible IDs。
projection 的 `account_schedulable_by_sub2api` 只包含持久的 enabled/health/credential状态，瞬时 slot满或排队状态在
选中时另行判断，不能写成 eligible=0。

override、eligibility、hold、attempt 和 revenue 外键一律使用稳定的 `listing_binding_id`。需要审计远端实例时另存
当时的 `listing_instance_id/a6_listing_id` 快照；远端 Listing 重建不得级联删除或迁移这些历史关系。

### 7.2 现有代码中的两个真实注入点

当前代码已经有 Delta/ActualCost 和下游 usage 两条链，但二者粒度不同且没有共用同一 attempt 冻结事实。

本地扣费现状：

```text
ManagedSellerService: rate_multiplier = 1 + delta
-> managed_sellerRepository 写 user_group_rate_multipliers(user_id, group_id)
-> GatewayService.recordUsageCore 解析 user/group multiplier
-> calculateRecordUsageCost
-> BillingService: ActualCost = TotalCost * RateMultiplier
-> buildUsageBillingCommand: BalanceCost = ActualCost
-> usageBillingRepository.Apply
-> deductUsageBillingBalance
```

对应代码锚点：

```text
backend/internal/service/managed_seller.go
backend/internal/repository/managed_seller_repo.go
backend/internal/service/gateway_usage_billing.go
backend/internal/service/billing_service.go
backend/internal/repository/usage_billing_repo.go
```

下游 A6 收益注入现状：

```text
billing_adjustment_outbound_openai.go
-> 改写返回给调用方的 usage
-> A6 作为调用方按改写后的 usage 计费
```

`billing_adjustment_outbound_openai_test.go` 已明确断言本地解析/计费保留原始 usage；
`billing_adjustmentEngine.adjust` 当前只计算和审计 `AdjustedActualCost`，没有把它写回 `CostBreakdown.ActualCost`。
现有 outbound adjustment 没有 1000 input usage 门限，测试中的 130 input tokens 仍会进入改写；A6 输出专属路径必须
增加本文规定的 1000 门限。
因此新实现不是继续使用旧 `(user, group)` 倍率，也不是让通用 Billing Adjustment 再随机计算一次，而是用同一
winner attempt 的经济快照统一驱动两条链。

### 7.3 Attempt 冻结和本地 ActualCost

每次 scheduler 选中账号后、发出上游请求前创建不可变快照：

```text
a6_output_attempt_economic_snapshots:
    id, request_id, attempt_id UNIQUE,
    a6_credential_id, local_user_id, output_channel_id, ingress_api_key_id,
    listing_binding_id, listing_instance_id,
    a6_listing_id_snapshot, a6_channel_id_snapshot,
    normalized_model, account_id,
    ck, sk, profit, delta_auto, delta_override, delta_effective,
    original_input_usage, delta_applied, adjustment_skip_reason,
    listing_generation, account_generation, route_generation,
    cost_generation, capability_generation, billing_generation,
    disposition=selected|dispatched|lost|won|billed|void,
    created_at, finalized_at
```

failover/retry 选择新账号时必须创建新快照，不能沿用上一账号的 delta。快照从 `selected` 进入 `dispatched` 前
再次核对全部 generation；不匹配则标记 void并重新选择。进入 `dispatched` 后持有 generation inflight 引用，最终
winner 始终按派发时快照改写 response usage 和扣费，不因期间出现新 generation 而改写历史。失败 attempt 标记
lost/void，不扣输出用户余额。新 generation 只阻止后续 attempt；旧 generation 等 inflight 清零后才能清理。

成功终态不得先返回 A6、再临时决定是否记账。non-stream response 或 stream 最终 usage event发出前，必须在一个
本地事务中：

```text
CAS economic snapshot: dispatched -> won
-> 持久化 original_usage + adjusted_usage + adjusted_usage_sha256
-> INSERT a6_output_winner_billing_intent UNIQUE(economic_snapshot_id)
-> COMMIT
-> 才向 A6 发出带调整 usage 的成功终态
-> 幂等 consumer执行 usage billing Apply并把 snapshot/intent标记 billed
```

事务失败时不发送可被 A6 按成功结算的最终 usage；进程在 COMMIT 后崩溃时由 intent consumer继续本地扣费。Replay
只能读取原 intent并重放未完成步骤，不能为同一 request另建第二个 winner。

对 `ToA6API` 流量：

```text
base_rate_multiplier = 1.0
original_input_usage = original usage.input_tokens or usage.prompt_tokens
delta_applied = 0 if original_input_usage < 1000 else delta_effective
economic_rate_multiplier = 1 + delta_applied
base_actual_cost = cost calculated with base_rate_multiplier
ActualCost = quantize_up(base_actual_cost * economic_rate_multiplier)
BalanceCost = ActualCost
```

门限使用改写前的总 input usage并包含cached input；缺少input/prompt usage时也跳过。低于1000时返回usage原文，
本地ActualCost与BalanceCost都保持base值，并记录 `input_usage_below_1000`。1000本身进入正常调整。该规则是明确的
小额请求例外，不能只跳过远端usage却继续放大本地ActualCost。

`ck/sk/profit/delta/economic_rate_multiplier/base_actual_cost/ActualCost/BalanceCost` 的决策值使用 Decimal并按 Sub2账本
精度向上量化后持久化为 NUMERIC。现有 Go `float64` 字段只能接收最终展示/兼容副本，不能反向参与 eligibility、
delta、价格写入或最终扣费判定。

将 `economic_snapshot_id/delta_effective/delta_applied/adjustment_skip_reason/economic_rate_multiplier` 写入 `UsageLog`
和 usage billing command。
结算幂等身份固定为 `UNIQUE(economic_snapshot_id)`，同一个 winner snapshot 最多产生一条 usage billing row；
`adjusted_usage_sha256` 只作为内容一致性校验，重复到达时 hash 不同必须报警并拒绝第二次结算，不能把不同 hash
当成新的 dedup identity。账号自身的
`AccountRateMultiplier`、上游成本统计和 `sk` 账本不乘 delta。A6 流量绕过通用全局 Billing Adjustment 和旧
`user_group_rate_multiplier`，防止二次加成。

### 7.4 返回 A6 的 usage 与真实收益

为 A6 输出流量新增确定性的 `A6OutputBillingAdjustmentPlan`。它复用现有 OpenAI non-stream/stream usage 改写
适配器，但目标由 winner 最终得到的 `delta_applied` 指定：

```text
base_merchant_earning_micros = price_vector dot original_billable_usage
target_merchant_earning_micros =
    ceil(base_merchant_earning_micros * (1 + delta_applied))

rewrite usage with endpoint-specific integer invariants
achieved_merchant_earning_micros = price_vector dot adjusted_billable_usage
```

要求：

1. `delta_applied=0` 时逐字节保持原usage；大于0时选择达到target的最小整数usage方案，舍入策略由实现按价格向量
   确定并记录overshoot。
2. 保持 Responses、Chat Completions 和流式终态各自的 `input/output/cache/total` 结构不变量；本地保存原始 usage
   和调整后 usage 两份事实，不用调整后 usage 估算真实上游消耗。
3. 计算使用该 Listing 已确认的 input/output/cache_read/cache_write/per_request micros 向量和 Decimal，禁止 float。
4. A6的per-token和per-request结算都会随返回usage改变；二者共用同一usage adjustment。`delta_transport_capable`
   只按endpoint adapter是否提供可改写usage计算，不按charge_type排除per-request。per-request Listing写价使用
   `per_request_price_micros`，其他四个Token价格字段写0并写后读回。
5. 通用 Billing Adjustment 的策略目标、随机审计采样和 A6 精确 delta 不得叠加；一个响应只允许一个 usage
   rewrite owner。

端到端收益证明不是只看响应 body。使用唯一 `request_id` 运行受控请求，并在同一测试保存：

```text
winner attempt snapshot
original and adjusted usage
Sub2 UsageLog.TotalCost / ActualCost / RateMultiplier
usage billing dedup row and BalanceCost
before/after negative user balance
A6 GET /api/supplier/earnings matched by request_id
A6 quota_micros / earned_micros / returned token dimensions
Listing price vector, commission, ck and FX generation
```

验收必须证明：A6收益明细使用调整后usage；`earned_micros`与Listing价格向量计算一致；本地ActualCost与同一
`delta_applied`一致；重复回调或Replay不产生第二次扣费；failover只采用winner账号的delta；999 input保持原文，
1000 input开始调整。

### 7.5 收益对账与单位边界

Sub2 `ActualCost/BalanceCost` 是本地输出 Channel 应收账本单位，A6 `earned_micros` 是远端商户净收益微单位；二者
不得直接比较绝对值。每个成功 winner 建立：

```text
a6_output_revenue_reconciliations:
    a6_credential_id, request_id, economic_snapshot_id UNIQUE,
    listing_binding_id, listing_instance_id,
    original_usage_json, adjusted_usage_json,
    base_actual_cost, actual_cost,
    expected_supplier_earned_micros,
    expected_public_quota_micros,
    observed_earned_micros, observed_quota_micros,
    commission_generation, price_vector_generation, fx_generation,
    status=PENDING|MATCHED|MISMATCH|PENDING_DELAYED|MISSING,
    first_checked_at, last_checked_at, matched_at, details_json

UNIQUE (a6_credential_id, request_id)
```

分别证明三个关系：

```text
local ActualCost / base_actual_cost ~= 1 + delta_applied（仅允许本地向上量化误差）
A6 adjusted supplier earning / A6 base supplier earning ~= 1 + delta_applied
A6 public quota / supplier earning ~= 1 + commission = 1.2
```

A6 两项使用 Listing 当前请求快照的实际整数 price vector、原始/调整 usage 和 A6 微单位规则计算；本地与远端只通过
同一 request/snapshot、usage、price vector 和 FX generation关联，不把 USD/CNY 或 micros 混成同一绝对数。

winner 结算后每 15 秒按 `request_id` 拉取 earnings，前 10 分钟保持 PENDING；随后标记 PENDING_DELAYED，并由
后台每 5 分钟继续对账 24 小时，仍不存在则标记 MISSING并进入每日 late-arrival sweep。纯粹的 earnings 延迟/缺失
只报警，不覆盖last-confirmed映射，也不自动下架。生产请求在15分钟内出现3个独立request_id不匹配，或单次商户
收益相对偏差超过5%，由输出配置控制器upsert `OUTPUT_CONFIG_INVALID`。修复价格、markup或usage映射并完成精确
读回后清除此hold；earnings继续异步对账，不作为Listing开放前置条件，也不允许管理员直接改成MATCHED。

### 7.6 价格方向性 Saga

- ck 下调：先发布一个保守过渡 generation，使所有新 attempt 按
  `dispatch_ck=min(last_confirmed_ck, desired_ck)` 计算 eligibility/delta；等旧 pricing generation 的 inflight=0，
  再 PUT 远端低价并精确读回，最后激活新的 confirmed pricing generation。等待超时则取消本次降价、保持远端
  confirmed ck并重新投影，绝不在旧 attempt 仍可能结算时先降远端价。
- ck 上调：先 PUT 远端高价并精确读回，再发布允许降低 delta 的新 generation；在途请求继续使用旧 delta只会
  产生保守的额外收益，不产生利润缺口。
- PUT 超时或结果不明时，停止该 Listing 的后续价格写入并 GET 精确读回。读回旧值则重放同一 operation identity，
  读回目标值则 adopt，读回第三值则以 observed reality 重新规划。期间新 attempt 只使用上述保守 ck，不能把未
  确认价格和新 delta 混入同一快照。

## 8. 调度、错误分类与 Listing 下架触发

1. 经济公式只做硬过滤，过滤后复用 Sub2API 原有调度策略。
2. 初选、手动指定、粘性、抢槽后重检、failover、retry 和 Replay 的每次 attempt 都经过相同硬门。
3. 满并发和排队不改变 eligible 数量，不触发远端下架。
4. 单 attempt 失败只处理对应账号或 route generation；所有候选耗尽后的最终结果才允许改变 Listing 状态。
5. 账号归因错误在当前请求 failover前先 CAS该账号/route generation为本地 fast-blocked并失效相关 Redis候选；
   请求路径只做这一本地摘流和 Outbox写入，远端 Token/Pin/Auto-Pool修复仍由对应 Reconciler执行。

三个“没有成功账号”的出口必须互斥：

```text
projection 在请求前结构性 eligible=0
    -> 只维护 NO_ELIGIBLE，不创建 LOCAL_FAILURE_COOLDOWN

eligible>0，但本次所有已派发 attempt 均以运行错误失败
    -> 按聚合终态决定 LOCAL_FAILURE_COOLDOWN / incident

eligible>0，但账号全部只是瞬时满并发或排队超限，未派发 attempt
    -> 普通 service_busy，不增加任何 hold、不下架
```

三个出口对调用方都返回现有 Sub2API 的普通服务暂时不可用响应，不泄露 ck、sk、profit、delta 或账号数量。

错误分类以 `/Users/haven/Documents/code/a6api-auto/errorlist.md` 为基础，其第 4 类统一命名为
`LISTING_COOLDOWN_TRIGGER`：

- `USER_SHORT_CIRCUIT`：用户输入、入口鉴权、内容策略、客户端取消；立即短路，不处罚账号、不扩池、不下架。
- `A6_ACCOUNT_RECOVERABLE`：额度、Provider Token、配置漂移；摘受影响账号并进入对应修复控制器。
- `UPSTREAM_AUTO_POOL_EXPAND`：Class1 当前 route generation 上游失败；摘该 Class1并扩池。
- `LISTING_COOLDOWN_TRIGGER`：平台/endpoint/egress、本地依赖、责任证据不足的裸 5xx/timeout/reset/未知错误，
  以及“确实派发过 eligible attempt 后全部失败”的聚合终态；最终请求失败后关闭该 Listing 本地准入并发出
  下架事件。结构性 eligible=0 和纯 busy 不属于本类。

`LISTING_COOLDOWN_TRIGGER` 描述“本地下架触发原因”，不得与 A6 回读的 `REMOTE_COOLING` 混用。

Class2/Class3 的上游鉴权、额度、健康和 cooldown 继续由 Sub2API 原生账号状态机处理，不触发 Class1 Auto-Pool
扩池；该账号被原生状态机摘流后只重算关联 eligibility。`unauthorized/invalid_api_key` 若发生在我方远端 Supplier
Channel 使用的 `output_channel_ingress_key`，属于输出配置故障：给父 Channel 全部 Listing增加
`OUTPUT_CONFIG_INVALID`，不能伪装成普通用户输入错误。

## 9. Listing 状态模型、控制器所有权与恢复

### 9.1 为什么删除 `PAUSED_REMOTE`

`PAUSED_REMOTE` 没有独立事实来源。它可能同时表示：

- 本地因最终请求失败而主动暂停；
- 本地希望上架，但远端被普通下架；
- 本地希望上架，但远端进入 cooling；
- 某次远端写入结果不明，正在等待 GET 读回。

这四种情况的进入条件、等待条件和恢复动作不同。把它们压成一个状态后，同一 Listing 会同时被本地 cooldown、
普通 relist 和远端 cooling reset 三个控制器接管。新设计中不保存 `PAUSED_REMOTE`，也不使用单一 `status`
字段表达全部事实。

### 9.2 四个正交事实

```text
active_local_holds: set<
    NO_ELIGIBLE
    | LOCAL_FAILURE_COOLDOWN
    | OUTPUT_CONFIG_INVALID
    | OUTPUT_KEY_ROTATION
    | BINDING_REVIEW
    | OPERATOR_HOLD
>

desired_remote_state:
    LISTED | UNLISTED

observed_remote_state:
    LISTED | UNLISTED | COOLING | UNKNOWN

reconcile_phase:
    IDLE | UNLISTING | WAITING | DIRECT_PREFLIGHT | CANARY_LISTING
    | PROBING | BATCH_LISTING | VERIFYING

reconcile_plan:
    NONE | ALIGN_UNLISTED | DIRECT_RELIST | COOLING_RESET
    | CANARY_RECOVERY | VERIFY_UNKNOWN | VERIFY_READINESS
```

另存：

```text
last_confirmed_observed_state
observed_callable=true|false|unknown
readback_stale
state_generation
reconcile_scope=LISTING|CHANNEL
recovery_operation_id
```

本地 hold 使用独立关系表，而不是 users/listing 上的单值枚举：

```text
a6_listing_holds:
    id
    listing_binding_id
    reason
    source_key
    owner_controller
    source_generation
    details_json
    activated_at
    updated_at

UNIQUE (listing_binding_id, reason, source_key)
```

同一 Listing 可以同时存在多个真实本地原因，例如 `NO_ELIGIBLE + OPERATOR_HOLD`，也可以同时存在多个
`OUTPUT_CONFIG_INVALID` source，例如 `commission_drift`、`output_key_mismatch` 和 `price_vector_invalid`。每个控制器
只能 upsert/delete 自己拥有的精确 `(reason, source_key)` 行；清除一个 source 不得影响同 reason的其他事实。

四个字段分别回答：

1. `active_local_holds`：本地当前有哪些独立原因不愿意卖。
2. `desired_remote_state`：按本地事实计算后，A6 Listing 应该上架还是下架。
3. `observed_remote_state`：A6 当前实际回读是什么。
4. `reconcile_plan + reconcile_phase`：单写者 Reconciler 要执行哪套计划、当前走到哪一步。

`desired_remote_state` 是投影结果，不由多个控制器随意写：

```text
if binding_lifecycle_state != ACTIVE
   or parent_channel_lifecycle_state != ACTIVE:
    desired_remote_state = UNLISTED
else if active_local_holds is not empty:
    desired_remote_state = UNLISTED
else:
    desired_remote_state = LISTED
```

`NO_ELIGIBLE`、`OUTPUT_CONFIG_INVALID`、`OUTPUT_KEY_ROTATION` 和 `BINDING_REVIEW` 分别由 eligibility、输出资源
校验、Channel Key 轮换和绑定控制器维护，因此 `active_local_holds` 为空已经代表 eligible>0、父 Channel、唯一
入口 Key、ToA6API、模型绑定等本地前置条件全部成立。Channel 级控制器用同一 source generation 为全部子
Listing upsert/delete 自己拥有的 hold，不通过一个模糊的用户暂停位替代 Listing 事实。

请求准入不是一个可写 boolean，而是派生模式：

```text
OPEN:
    binding_lifecycle_state = ACTIVE
    AND parent_channel_lifecycle_state = ACTIVE
    AND active_local_holds is empty
    AND desired_remote_state = LISTED
    AND observed_remote_state = LISTED
    AND observed_callable = true
    AND reconcile_plan = NONE
    AND reconcile_phase = IDLE
    AND request_generation matches current state_generation

PROBE_ONLY:
    current recovery operation explicitly owns the resource lock
    AND request carries the current epoch's valid HMAC probe fingerprint
    AND requested model/listing/account match the operation

CLOSED:
    every other combination
```

该值计算为 `effective_local_admission_mode`，不作为可被人工单独修改的字段。`PROBE_ONLY` 只放行当前 operation
的一次受控探针；普通 A6 流量即使使用同一个 Channel Key 也继续得到普通服务暂时不可用响应。

普通 GET 暂时失败时保留 `last_confirmed_observed_state` 和 `observed_callable`，只设置 `readback_stale=true`，
沿用最近确认结果。只有远端写结果不明时才把 observed 置为 `UNKNOWN`、phase 置为 `VERIFYING`，并关闭
受影响 Listing 的有效准入，直到精确 GET 收口。

### 9.3 派生业务状态

以下名称只用于管理端展示和指标，不作为第二套可写状态：

- `STAGED`：binding 或父 Channel 尚未进入 ACTIVE lifecycle。
- `RETIRED`：binding 或父 Channel 已进入 RETIRED lifecycle。
- `ACTIVE`：两级 lifecycle均为 ACTIVE、holds 为空、desired=LISTED、observed=LISTED、callable=true、plan=NONE、phase=IDLE。
- `PAUSED_NO_ACCOUNT`：holds 包含 NO_ELIGIBLE。
- `COOLING`：holds 包含 LOCAL_FAILURE_COOLDOWN。
- `OUTPUT_CONFIG_REVIEW_REQUIRED`：holds 包含 OUTPUT_CONFIG_INVALID。
- `OUTPUT_KEY_ROTATING`：holds 包含 OUTPUT_KEY_ROTATION。
- `BINDING_REVIEW_REQUIRED`：holds 包含 BINDING_REVIEW。
- `OPERATOR_HOLD`：holds 包含 OPERATOR_HOLD。
- `REMOTE_HOLD`：两级 lifecycle均为 ACTIVE、holds 为空、desired=LISTED、observed=UNLISTED。
- `REMOTE_COOLING`：两级 lifecycle均为 ACTIVE、holds 为空、desired=LISTED、observed=COOLING。
- `REMOTE_UNKNOWN`：两级 lifecycle均为 ACTIVE、holds 为空、desired=LISTED、observed=UNKNOWN；只允许出现在 VERIFYING 阶段。
- `REMOTE_UNCALLABLE`：两级 lifecycle均为 ACTIVE、holds 为空、desired=LISTED、observed=LISTED，但 callable 不是 true。

本地 hold 标签允许同时展示，例如 `PAUSED_NO_ACCOUNT + OPERATOR_HOLD`，因为它们是并存事实；但它们只共同
产生一个 `desired=UNLISTED`。所有 `REMOTE_*` 状态都要求 holds 为空，因此不会和本地 `COOLING`、
`PAUSED_NO_ACCOUNT` 等状态竞争恢复控制权。

`RECOVERING` 不是业务状态。管理端把 `reconcile_plan/reconcile_phase` 作为第二列或副标签同时展示，例如：

```text
COOLING / ALIGN_UNLISTED:UNLISTING
REMOTE_HOLD / DIRECT_RELIST:CANARY_LISTING
REMOTE_COOLING / COOLING_RESET:WAITING
REMOTE_UNKNOWN / VERIFY_UNKNOWN:VERIFYING
REMOTE_UNCALLABLE / VERIFY_READINESS:PROBING
```

删除 `PAUSED_REMOTE`、`PAUSED_BINDING`、`BINDING_BROKEN` 和 `REMOTE_COOLING_PERSISTENT`。绑定问题同 owner
且唯一匹配时自动修正；刷新 inventory 后可唯一重绑时自动重绑；只有 owner 冲突、多候选或资源确实消失时
进入 `BINDING_REVIEW_REQUIRED`，管理员可重新绑定、接受远端事实或退役。

Remote Observer 必须按真实 A6 字段投影，而不是假设远端有一个 `callable` 或 `cooling_epoch`：

```text
Supplier Listing listed:
    status=2 AND visibility=1 AND availability=1 AND down_reason=""
Supplier Listing unlisted:
    status=0 AND visibility=0 AND availability=0

GET /api/marketplace/circuit-states item:
    scope, channel_id, model, state, remaining_seconds,
    probe_status, last_probe_at, probe_success_streak, probe_success_required,
    next_probe_at, cooling_until, last_cooling_at,
    last_error_kind, last_error_message, recovery_mode
```

保存 Supplier Listing 和 Supplier Channel 的完整原始状态 tuple。除上述已确认组合外，任何审核中、拒绝、未知枚举
或互相矛盾的组合先投影为 `observed_remote_state=UNKNOWN` 和 admission=CLOSED，由 `VERIFY_UNKNOWN` 定向读回；
不得把未知状态当成普通 UNLISTED 反复 resubmit。连续两次 fresh GET仍为同一未知组合时设置
`operator_attention=true`，保留人工“接受远端事实/重试当前 desired/退役”出口。

`state=cooling|half_open` 投影为 `observed_remote_state=COOLING`；`state=model_excluded` 在 Supplier Listing 仍上架
时投影为 `observed_remote_state=LISTED, observed_callable=false`，reason 为 `model_excluded`。`scope=channel` 影响
父 Channel 下全部 Listing，`scope=model` 只影响规范化模型匹配的 Listing。没有 circuit item 只表示没有当前
非 closed 记录，仍需结合 Supplier Listing、marketplace `listing_availability` 和数据面证据。

`observed_callable` 是以下事实的派生值，不是远端单字段：Supplier Listing 组合、marketplace 对应
`listing_id/channel_id/model` 的 availability、Supplier Channel 状态、circuit 状态、入口 Key/模型读回，以及
需要时的受控生产成功或 Probe。

### 9.4 控制器所有权和互斥规则

控制器严格分工：

1. Eligibility、输出配置、绑定、本地 cooldown 和管理员操作只增删自己拥有的 hold reason 及其 generation。
2. Desired Projector 只根据最新本地事实生成 `desired_remote_state` 和 Outbox。
3. Remote Observer 只写 `observed_remote_state`、callable 和 readback 证据。
4. Recovery Planner 只根据当前 generation 的 local/desired/observed 选择一个 `reconcile_plan`，自身不写远端。
5. Output Recovery Reconciler 是远端 Channel/Listing 上下架的唯一写者，只执行 Planner 选定的 plan。
6. Remote Observer 和精确 GET 是 plan 完成与否的唯一远端事实来源。

远端恢复操作使用独立表：

```text
a6_output_recovery_operations:
    id, scope=LISTING|CHANNEL,
    output_channel_id, nullable_listing_binding_id,
    source_epoch, source_generation, plan, phase,
    canary_listing_binding_id, admission_mode,
    temporary_remote_override=NONE|LISTED_CANARY,
    desired_hash, lease_owner, lease_expires_at,
    disposition=PENDING|RUNNING|SUCCEEDED|FAILED|SUPERSEDED

UNIQUE active (scope, scoped_resource_id)
UNIQUE (scope, scoped_resource_id, source_epoch, plan)
```

`circuit-states.scope=channel` 时只创建一个 CHANNEL operation并持有父 `output_channel_id` 资源锁；它投影全部子
Listing 的 observed state、选择一个 canary并统一下架/恢复，子 Listing不得各自创建 `COOLING_RESET`。
`scope=model` 才创建单个 LISTING operation。锁顺序固定为 Channel -> Listing，禁止反向获取。

`LISTED_CANARY` 是 Reconciler在 current operation资源锁内对 desired=UNLISTED 的唯一临时例外，必须同时绑定
`admission_mode=PROBE_ONLY` 和一个 canary；operation结束、超时、失租或被 supersede 时先 takedown/readback，再
清除 override。Desired Projector本身始终保留真实 UNLISTED，不因探针临时改写业务意图。

Planner 的决策顺序固定为：

```text
if a current-generation recovery operation already exists:
    plan = that operation's plan
else if observed == UNKNOWN:
    plan = VERIFY_UNKNOWN
else if desired == UNLISTED and observed != UNLISTED:
    plan = ALIGN_UNLISTED
else if desired == UNLISTED and observed == UNLISTED
        and an owning hold controller has emitted a current-generation recovery request:
    plan = CANARY_RECOVERY
else if desired == UNLISTED and observed == UNLISTED:
    plan = NONE
else if active_local_holds is not empty:
    # desired 必须已经是 UNLISTED；若不是，拒绝生成 plan 并报警
    plan = NONE
else if desired == LISTED and observed == COOLING:
    plan = COOLING_RESET
else if desired == LISTED and observed == UNLISTED:
    plan = DIRECT_RELIST
else if desired == LISTED and observed == LISTED and observed_callable == true:
    plan = NONE
else if desired == LISTED and observed == LISTED:
    plan = VERIFY_READINESS
else:
    plan = VERIFY_UNKNOWN
```

`COOLING_RESET` 和 `CANARY_RECOVERY` 都是原子执行计划，内部允许按
`UNLISTING -> WAITING -> DIRECT_PREFLIGHT -> CANARY_LISTING -> PROBING -> VERIFYING -> BATCH_LISTING` 前进。
即使中途 observed 变成 UNLISTED，也不得另建
`DIRECT_RELIST`；只有当前 plan 完成、失败或被新 generation supersede 后，Planner 才能重新选 plan。

最重要的仲裁规则：**任何 active local hold 都压过普通远端恢复。** hold 集合非空时，旧的 relist/reset 任务必须
停止或被新 generation supersede；通常只允许 `ALIGN_UNLISTED`。唯一例外是该 hold 的 owning controller 在远端
已经精确 UNLISTED 后发出的 current-generation `CANARY_RECOVERY`，它可在资源锁内临时设置一个 Listing 为
`PROBE_ONLY`，但不能开放普通流量。只有 hold 清除后，Planner 才执行普通 DIRECT_RELIST/COOLING_RESET。

因此同一 Listing 不会同时得到三个恢复动作：

- `COOLING` 时业务层只执行本地 cooldown 恢复；远端层最多执行 `ALIGN_UNLISTED`，不生成 relist/reset plan。
- `REMOTE_HOLD` 只执行 `DIRECT_RELIST`，不等待本地 cooldown，也不执行 cooling reset。
- `REMOTE_COOLING` 只执行 `COOLING_RESET`，不另建普通 relist。
- `REMOTE_UNKNOWN` 只执行 `VERIFY_UNKNOWN` GET 读回，不盲目重写。
- `REMOTE_UNCALLABLE` 只执行 `VERIFY_READINESS` 的控制面检查或有界 Probe；确认 callable 后结束 plan。

### 9.5 本地 `COOLING` 完整流程

```text
请求所有候选耗尽，最终分类为 LISTING_COOLDOWN_TRIGGER
-> upsert hold reason LOCAL_FAILURE_COOLDOWN
-> state_generation + 1
-> desired=UNLISTED
-> 立即关闭本地准入
-> Outbox
-> Planner 选择 ALIGN_UNLISTED
-> Output Recovery Reconciler unlist/readback
-> 等待本地 next_local_recovery_at
-> 本地 readiness通过后，Local Cooldown Controller 发 current-generation recovery request
-> Planner 选择 CANARY_RECOVERY
-> 按第 9.8 节执行 direct preflight + PROBE_ONLY A6 canary
-> canary 通过后生成 recovery witness
-> Local Cooldown Controller只删除 LOCAL_FAILURE_COOLDOWN hold
-> 若仍有其他 hold，desired 保持 UNLISTED，结束本轮本地恢复
-> 若 hold 集合为空，Desired Projector 生成 desired=LISTED 的新 generation
-> Planner 根据最新 observed 选择 DIRECT_RELIST、COOLING_RESET、VERIFY_UNKNOWN、VERIFY_READINESS 或 NONE
```

本地 cooldown 控制器不直接 relist。它只负责取得恢复证据并删除自己拥有的 hold，远端写入仍由 Output Recovery Reconciler
独占，这正是避免重复恢复动作的关键。

本地字段：

```text
local_cooldown_epoch
local_failure_fingerprint
local_recovery_attempt
next_local_recovery_at
```

默认退避为 15 秒、120 秒、10 分钟并封顶 10 分钟。成功、failure fingerprint 改变或 state generation
切换后重置 attempt。删除含义不清的通用 `cooldown_level` 和共享 `retry_count`。

### 9.6 `REMOTE_HOLD` 完整流程

进入条件必须同时满足：

```text
active_local_holds is empty
desired_remote_state=LISTED
observed_remote_state=UNLISTED
```

它表示 A6 被普通下架或远端状态漂移，不表示本地发生过故障。处理：

```text
Planner 选择 DIRECT_RELIST
-> 校验父 output Channel、唯一入口 Key、ToA6API、模型绑定、eligible projection 和当前 generation
-> admission_mode=PROBE_ONLY
-> Output Recovery Reconciler POST resubmit/relist
-> 使用第 9.8 节 A6 consumer canary验证数据面和收益
-> 精确读回 LISTED + callable
-> admission_mode=OPEN
-> ACTIVE
```

写结果不明则进入 `REMOTE_UNKNOWN / VERIFYING`；回读为 COOLING 则进入 `REMOTE_COOLING`；绑定不一致则由
Binding Controller 设置 `BINDING_REVIEW`，随后 desired 自动变为 UNLISTED。

### 9.7 `REMOTE_COOLING` 完整流程

进入条件必须同时满足：

```text
active_local_holds is empty
desired_remote_state=LISTED
observed_remote_state=COOLING
```

保存独立远端字段：

```text
remote_cooling_epoch
remote_cooling_until
remote_last_cooling_at
remote_last_error_kind
remote_cooling_scope
remote_reset_attempt
next_remote_retry_at
```

`scope=channel` 时这些字段和 epoch保存在 Channel recovery operation并投影到全部子 Listing；`scope=model` 时保存在
对应 Listing operation。不得让同一个 Channel epoch在每个子 Listing各自增加 `remote_reset_attempt`。

A6 当前没有独立 epoch 字段。`remote_cooling_epoch` 必须由规范化的
`(scope, channel_id, model, state, cooling_until, last_cooling_at, last_error_kind)` 做稳定 hash 生成；相同 hash
只能有一个 reset operation，任一组成字段变化才建立新 epoch。

处理：

```text
Planner 选择 COOLING_RESET
-> 关闭有效准入
-> Output Recovery Reconciler remote unlist/readback
-> 等待 max(now+15s, remote_cooling_until+jitter)
-> direct preflight
-> 仅 relist canary并设置 PROBE_ONLY
-> A6 consumer canary
-> canary readback/earnings通过
-> scope=model 时开放该 Listing；scope=channel 时按批次恢复其余无 hold Listing
-> 确认 Supplier Listing status=2/visibility=1/availability=1
-> 确认 circuit 不再阻断且派生 callable=true
-> ACTIVE
```

同一 remote cooling epoch 只运行一个任务；5 分钟内复发依次等待 120 秒、10 分钟，随后以 10 分钟封顶并
设置 `operator_attention=true`，但继续保留明确的人工重放和后续自动重试出口。

### 9.8 下架后的统一 Canary 闭环

Listing 处于 UNLISTED 时，A6 市场流量进不到本地，因此“先 A6 Probe、后 relist”是循环依赖。所有本地 cooldown、
REMOTE_HOLD、REMOTE_COOLING、Channel Key 轮换和平台 incident 恢复统一使用：

```text
1. 保持远端 UNLISTED、普通 admission=CLOSED
2. 用 output_channel_ingress_key + current operation签名经内部 control path直连 Sub2API 做 direct preflight
3. direct preflight 成功后，选择一个 eligible>0，且除本次 recovery owner持有的 reason外没有其他 blocking hold的
   canary Listing
4. 在资源锁内把该 Listing admission设为 PROBE_ONLY
5. 远端 resubmit/relist canary并精确读回 LISTED
6. 用专用 A6 recovery consumer Token Pin 到
   (canary a6_channel_id, normalized_model)
7. 发出带当前 operation HMAC fingerprint 的一个有界 Probe
8. 同时核对响应终态、实际 winner/generation、Supplier Listing、circuit state和
   /api/supplier/earnings request_id
9. 成功：产生不可变 recovery witness；失败：立即 takedown/readback并进入本 operation退避
10. LISTING scope 开放该 Listing；CHANNEL scope 以默认每批 10 个、批间 5-15 秒 jitter恢复其余无 hold Listing
```

direct preflight 仍执行真实 Key鉴权、model解析、eligibility和 scheduler，只允许 current operation签名绕过
`observed_remote_state!=LISTED` 这一项，不得绕过 NO_ELIGIBLE、账号健康或经济硬门。它只证明本地 Key -> Listing解析
-> scheduler -> 上游账号链路，不计作 A6 收益证明；A6 consumer
Probe 才证明 Marketplace -> 我方 Channel -> Sub2 -> winner -> usage rewrite -> earnings 的完整链路。专用 recovery
consumer Token 是独立控制资产，使用独立表和 duty，禁止复用 `provider_route_token` 或
`output_channel_ingress_key`。其 Pin、Key 和使用记录都绑定 recovery operation，完成后解除 Pin或恢复原配置。
Token 可从受控池串行复用，但必须取得独占 lease，保存 Pin前快照，并在成功、失败、超时或接管后精确恢复；
没有可用 recovery consumer Token只延迟 Probe，不允许临时借用生产 Provider Token。
Token名称同样从 `token_name_corpus.json` 随机选择，具体 `recovery_consumer` duty只存控制面，不编码进名称。

HMAC fingerprint 至少覆盖 `operation_id/output_channel_id/listing_binding_id/model/source_epoch/nonce`，只在当前
operation 的短 TTL 内有效。同一 operation只接受一个成功 Probe；普通生产请求、旧 epoch Probe和模型不匹配请求
在 PROBE_ONLY 阶段都不进入 scheduler。若 Channel scope 没有任何无 blocking hold且 eligible 的子 Listing，结束 operation
并保持远端下架，不发 Probe。

若 `CANARY_RECOVERY` 成功后 owning hold 之外仍有其他 hold，先把 canary重新下架/readback，再结束 operation；
不得因为探针成功绕过 NO_ELIGIBLE、OPERATOR_HOLD、BINDING_REVIEW 或其他控制器的事实。

### 9.9 并发和状态变化示例

若 Listing 正处于 `REMOTE_HOLD / DIRECT_RELIST:CANARY_LISTING`，此时 eligible 归零：

```text
Eligibility Controller upsert NO_ELIGIBLE hold
-> state_generation + 1
-> desired 变为 UNLISTED
-> 旧 DIRECT_RELIST plan 被 supersede
-> 同一资源锁下只执行新 ALIGN_UNLISTED plan
```

若 Listing 正处于本地 `COOLING`，A6 同时回读 COOLING：本地标签仍包含 `COOLING`，Planner 因 hold 集合
非空只生成 `ALIGN_UNLISTED`，不生成 `COOLING_RESET`。等本地恢复证据通过、删除
`LOCAL_FAILURE_COOLDOWN` 且其他 hold 也全部清除后，Planner 再根据最新 A6 readback 决定是否选择
`COOLING_RESET`。这是 generation 之间的顺序转换，不是两个恢复计划并行执行。

## 10. 候选集、上下架和恢复证据

1. 经济、模型或账号健康意义上的候选集变为 0：Eligibility Controller 立即 upsert `NO_ELIGIBLE` hold。
   Desired Projector 生成 UNLISTED，新 generation 关闭本地准入并异步下架。
2. 候选恢复 `0 -> >0`：先验证父 output Channel、本地用户、唯一 Channel Key、ToA6API、Listing/模型绑定、
   远端 route 和 projection generation，
   然后只删除 `NO_ELIGIBLE` hold。若还有其他 hold，desired 继续为 UNLISTED；只有 hold 集合为空时，
   Desired Projector 才生成 LISTED，新 generation 由 Output Recovery Reconciler 上架/readback；确认 observed LISTED
   且 callable 后开放。
3. 请求路径只写本地状态和 Outbox；不得直接调用远端上下架、Ban、Pin、Auto-Pool 或 Token 写操作。
4. 恢复证据优先级：匹配当前 generation 的新鲜生产成功 -> 控制面/readiness/readback -> 第 9.8 节单个有界
   A6 consumer Probe。远端已经 UNLISTED 或本 operation执行过 unlist 时，旧生产成功不证明重新上架后的链路，
   必须走 PROBE_ONLY canary。
5. 可复用生产成功必须匹配当前 output_channel/listing/account/model/route/endpoint generation，发生在最后失败和配置变更后，
   120 秒内，terminal completed，输入 500-3000 tokens，输出至少 8 tokens。
6. Probe 输入上下界固定为管理员参数，默认 500 和 3000 tokens；另提供全局并发、每分钟请求数和每小时成本
   上限参数。达到任一预算只延迟恢复 Probe，不绕过准入条件。
7. recovery witness 必须记录 `traffic_kind`、operation/source epoch、output channel、listing binding/instance、A6
   consumer token、winner account、全部 generation、原始/调整 usage、earnings request_id 和 readback hash；旧 epoch
   witness 不能恢复新 generation。
8. Probe内容只从版本化探针库选择；以 HMAC 对 `operation epoch/account/model/route generation` 选样，同一
   `(account, model)` 最近 20 次不重复模板。`traffic_kind=recovery_probe|billing_audit_probe`，从普通生产成功率、
   Auto-Pool稳定成功计数和客户用量中排除，但其真实上游成本、A6 earnings和全局 Probe预算必须记账。
9. direct readiness可按相同 `(account, model, route_generation, endpoint_generation, failure_fingerprint)` 共享；A6
   earnings canary包含具体 output Channel/Listing Key链路，LISTING scope不能借用其他 Listing的 earnings。CHANNEL
   scope只允许同一父 Channel的 canary作为 Channel级恢复证据，其余子 Listing仍需各自控制面 readback。

## 11. Provider 故障与 liquidity

1. Class1 `insufficient_quota` 立即摘流并标记 UNFUNDED，进入 Trader Provider liquidity 流程，不扩 Auto-Pool。
2. 先把该 merchant 可提现余额中的整单位划转到用户余额并读回；仍不满足 Provider 合同时移除 Provider duty，
   原槽位 VACANT，请求已注资替补。
3. 替补只有在 owned Ban、Observer-derived range、Enodot cost、模型能力和 schedulability 全部读回后才能入槽。
4. liquidity/callback Token 与 Provider Token 独立，名称来自 `token_name_corpus.json`；没有 listed/callable 的我方
   small-merchant Channel 时跳过划转请求。
5. Provider Token失效/禁用、无限额度配置漂移、Pin写入/读回失败等 `A6_ACCOUNT_RECOVERABLE` 先摘该 Class1，
   分别进入第 4.1 节 Token轮换或窄 Pin/配置 Reconciler，不扩 Auto-Pool。只有分类为
   `UPSTREAM_AUTO_POOL_EXPAND` 的其他 Class1运行错误才走扩池，且不要求实际 channel_id归因。
6. 生产 liquidity/callback Responses请求继续使用现有精确输入
   `PING 5db98307ce2d442ee568baacf8b0e7076fa657cbe2806ccff9da6386997915e2`，并验证现有 realistic sampled usage与
   high-cache不变量；`prompt_tokens=1/completion_tokens=0/total_tokens=1` 等 probe-shaped结果不得记作有效划转。
   recovery consumer Probe和 liquidity/callback流量使用不同 token duty、traffic_kind和账本。

## 12. 平台 Incident 的 fingerprint 与批量恢复

依据 `errorlist.md`：

- `cf_1010`、`cloudflare_forbidden`、`endpoint_unavailable`、`platform_overloaded`、明确 A6 全局限流，或
  DB/Redis/Relay/Scheduler 健康检查失败，可直接建立相应 scope 的 incident。
- 单次裸 5xx、timeout、reset、missing_terminal_event 或 unknown_error 先作为当前 Listing 的
  `LISTING_COOLDOWN_TRIGGER`，不得直接扩大为全平台。
- 模糊错误只有在同一请求至少两个独立 account/route identity 经相同 endpoint 得到同一 fingerprint，或
  60 秒内第二个独立 Listing 得到同一 fingerprint 时，才提升为 endpoint/platform incident。
- 只有一个候选时，必须等待控制面健康证据或另一个独立 Listing 证据。

fingerprint 至少由以下标准化字段生成：

```text
failure_domain | canonical_error_code | endpoint_host | endpoint_path_class |
http_method | egress_region | relay_generation | credential_scope
```

不得包含 listing_id/account_id/channel_id 等会阻止同源事件合并的字段；这些身份作为 incident members 保存。
其他 Listing 只有 fingerprint 和 scope 同时匹配才并入。

Incident 是协调层，不是第二个随意删除 hold 的控制器。提升为 incident 时，把匹配的
`LOCAL_FAILURE_COOLDOWN` hold 关联 `incident_id/fingerprint`，暂停其单 Listing定时恢复；hold 仍由 Local Cooldown
Controller拥有。Incident Reconciler按 scope 获取 endpoint/platform -> Channel -> Listing 锁，使用第 9.8 节
PROBE_ONLY canary生成 recovery witness，再把 witness 发送给各 hold owner，由 owner核对 source generation 后清除
自己的 hold。无法匹配、已换 generation 或另有原因的 hold 保持不动。

平台恢复时按 `platform_recovery_batch_size` 分批，默认 10；批次间加入 5-15 秒 jitter。每批先恢复一个 canary，
readback/Probe/earnings 成功后再恢复余项；任一 canary 失败立即停止后续批次并更新 incident 证据。相同远端
`scope=channel` 的多个子 Listing在批次中只占一个 Channel recovery operation，不能并发执行多个 reset。

## 13. 触发事件、频率和 Reconciler 合同

### 13.1 事件与频率

- 每个请求 attempt：同步执行 eligibility generation 硬门。
- requested model 解析：账号选择前同步固定 output Channel 和 Listing generation；解析失败不进入 scheduler。
- eligible `>0<->0`、管理员参数、Channel/Listing 绑定、入口 Key、route、模型能力和账单状态变化：提交后立即发 Outbox。
- Channel 级事件按一个 source generation 扇出全部子 Listing；模型级事件只重算匹配 Listing。
- Class1 marketplace/Top-N/A6 FX snapshot：每 60 秒；运行错误立即触发 fresh snapshot。
- Frankfurter 只在 A6 FX 不可用时启用，成功值缓存 30 分钟；fallback active 时每 15 分钟后台刷新一次，
  单次超时 3 秒。
- A6 platform-model 目录：每 5 分钟及管理员手动刷新；版本变化立即重算相关模型。
- `marketplace/circuit-states`：有 ACTIVE/REMOTE 状态 Listing 时每 15 秒；收到 cooling/route 错误后立即做一次
  定向 fresh read；没有任何输出 Listing 时不轮询。
- Supplier Listing/Channel 增量 readback：每 60 秒；完整分页 inventory 对账：每 5 分钟。
- Class2 usage：复用 Sub2API 现有统计刷新，不新增周刷新任务。
- Class3 账单偏差：15 分钟窗口。
- 普通定价事件按 `(model, source_generation)` 合并 30 秒；安全硬界、成本上涨和 eligible 归零立即执行。
- 价格写入每批默认 10；单 Listing 普通改价最短 5 分钟；写后逐项精确读回。

所有轮询按 `a6_credential_id` 共享：一次 circuit/inventory/platform-model响应扇出到该凭据的全部 Channel/Listing，
不得逐 Listing重复抓取。提供可配置的 A6 控制面全局/每凭据并发与 RPS上限，默认每凭据并发 2、全局并发 10；
HTTP 429 或平台限流只延后相应读任务并沿用 last-confirmed事实，不生成虚假的 down/cooling事件。

事件去重与防风暴规则：

1. attempt error 以 `attempt_id` 去重；只有 request aggregator 可以按 `request_id + listing_binding_id` 提交一个最终
   terminal event。后到的重复 stream/error callback 只补证据，不再次增加 cooldown attempt。
2. Eligibility 每个请求同步判定，但只有 projection generation 变化或 eligible `0<->>0` 时发状态事件；普通
   eligible 数量在正数区间内变化只更新 projection/指标。
3. circuit snapshot 规范化后按 `(scope, scoped_resource, remote_cooling_epoch)` 去重；15 秒轮询读到相同 hash 不创建
   新 Outbox、reset attempt 或 Probe。
4. Channel 级 Key、owner、Channel state或 `scope=channel` cooling 事件先在父 Channel 合并，再用一个 source
   generation 投影子 Listing；不得由每个子项反向生成相同 Channel 写操作。
5. 定价、模型目录和 FX 事件按文中的合并窗口取最新 desired hash；中间 generation 标记 superseded，不逐个写远端。
6. 每 5 分钟运行一次只读 drift sweep，按数据库 desired、远端 observed和 Outbox 状态补发漏事件；它只修复缺失
   operation，不对已经 pending/running 的相同 desired hash 重复入队。
7. Class1 扩池以 `(account_id, route_generation, failure_fingerprint)` 唯一；一个 generation的首个失败暂停账号并
   创建一次 target_n+1，随后并发失败只追加证据。Listing本地 cooldown以
   `(listing_binding_id, state_generation, local_failure_fingerprint)` 唯一，同一批并发终态只建立一个 epoch和一次退避。

每个事件都带 source version/generation；相同资源、相同 desired hash 只产生一个 pending operation。

### 13.2 Reconciler

1. 对同一远端资源使用数据库租约/资源锁，默认 lease 30 秒、每 10 秒续租；多实例只有持有者可写。锁层级固定为
   credential -> Provider Token 或 output Channel -> Listing，平台 incident在最外层；禁止逆序获取。
2. Outbox 以 `(resource_type, resource_id, desired_generation, operation_kind)` 唯一；旧 generation 在执行前标记
   superseded，乱序消息不得覆盖新事实。
3. 每次写入后必须 GET 精确读回；传输结果不明优先 readback，不计作普通盲重试。
4. 可重试故障默认最多 8 轮，指数退避并封顶 30 分钟；超过后进入可见 dead-letter，保持资源在保守准入状态。
5. 管理员可以从原 operation identity 重放 dead-letter；重放前重新读取当前 desired generation，禁止复活旧意图。
6. pricing/route/output recovery operation 都维护 inflight 引用；租约丢失只转移执行权，不释放仍在运行的 attempt
   generation。接管者先读 operation journal和远端状态，再从最后一个已确认 phase继续。

## 14. 管理端

1. `/admin/users` 在现有页面基础上增加 a6-channel 过滤。每行代表一个输出 Channel 用户，展示远端 Channel、
   Listing/模型数量、ACTIVE/UNLISTED/COOLING 数量、eligible 汇总、负余额、最后远端读回和唯一入口 Key 状态。
2. 用户抽屉先按 Listing/模型展示 `ck`、公开倍率、价格上下限、eligible 数、delta 范围和正交状态；展开后再展示
   逐 Sub2 账号的 class、sk、profit、delta_auto、delta_override、硬门结果、失败原因和 generation。
3. 输入部门单独管理 Class1/2/3、Provider Token、route mode、Ban attestation、sk、profit、模型能力和账单。
4. 输出部门单独管理 output Channel 用户、Channel 入口 Key、子 Listing、逐模型价格边界、分布、上下架、
   cooling 和 A6 收益核对。
5. 所有危险操作显示将修改的具体部门和远端资源，禁止使用只有“轮换 Key”“暂停”之类的模糊按钮。
6. 管理员可在逐账号明细编辑/清空 `delta_override`，编辑 class profit默认/账号 override、Class2
   `account_cost_cny/expected_weekly_quota_multiplier`、Class3 declared sk和逐模型 override，以及逐模型 Listing硬
   上下限；每次修改先显示公式重算后的 eligible变化，再提交新 generation。清空 delta_override立即回到动态值，
   不删除历史审计行。

## 15. 迁移

1. 迁移颗粒度按远端 A6 Channel，不按 Listing。先把 Trader 的
   `enodot_managed_seller_resources`、`a6_managed_channel_bindings`、`a6_managed_listing_bindings`，Sub2 的
   `a6_trader_managed_sellers/users/api_keys`、最近成功入口日志和实时 Supplier inventory 联合生成只读二部图：

```text
left:  old managed seller user + API Key
right: remote owner + a6_channel_id + all a6_listing_id/model
edge:  explicit binding | remote key last4/fingerprint | verified successful request
```

2. 对每个远端 Channel 选择且只选择一个 anchor 本地用户/Key。优先级是：明确 active binding -> 唯一匹配远端
   `api_key_last4` 且有成功入口日志 -> 在所有 Listing下用受控 A6 canary确认实际命中的 local UserID。证据仍多义
   时进入迁移 `BINDING_REVIEW`，不按最小 ID、创建时间或 Key 数量猜测。
3. 只有一个旧用户映射该 Channel 时，原地保留 `users.id` 和当前有效 Key，改为
   `user_type='a6-channel'`。多个旧 Managed Seller 用户映射到同一 Channel 时，保留 anchor 的 `users.id`/Key，
   把该 Channel 的全部 Listing binding 合并到 anchor；其他旧用户/Key在切换成功后设为 inactive/retired。历史
   usage、余额和审计行保留原 user_id，不做破坏性改写。
4. 一个旧用户/Key映射多个远端 Channel 时，它只能成为一个 Channel 的 anchor，优先选择已有实际入口证据的
   Channel；其余每个 Channel 创建新的 a6-channel 用户和唯一入口 Key，使用第 3.3 节 Saga逐个更新远端。任何
   时刻都不得让一个 API Key鉴权后再按 model 或 Channel动态切换本地 UserID。
5. 每个 anchor 建立稳定的 `a6_output_channels`、当前 `a6_output_channel_instances`，以及该 Channel 下全部稳定
   `a6_listing_bindings` 和远端 `a6_listing_instances`。约束 current remote instance唯一和
   `UNIQUE(output_channel_id, normalized_model)`；owner/channel/model或 normalization collision进入
   `BINDING_REVIEW`。
6. 旧 `user_group_rate_multipliers=1+delta` 不进入 ToA6API。ToA6API 和输出用户分组倍率统一回填为 1.0；旧
   delta 保存到迁移证据，并为切换时已有的 `(listing, account, model)` 生成带
   `source='legacy_managed_seller'`、引用稳定 `listing_binding_id` 的 nullable override 行以保持行为，管理员清空后
   回到动态 delta。
7. 删除 `999999999999` Managed Seller 余额 CHECK/触发器，记录所有参与合并/拆分用户的迁移前余额审计快照，
   并在切换输出账本时把新的
   Channel 用户余额置为 0；之后所有 winner ActualCost 正常把余额扣成负数。
8. 每个远端 Channel 切换前下架其 Listing并读回；唯一 Key、model解析、Ban、eligibility 和 canary全部通过后再
   上架。一个 Channel失败只回滚该 Channel，不阻塞已经完成的其他 Channel。
9. 现有 A6 Provider 迁移为 Class1 和独立 `provider_route_token` 绑定，不得与输出 Channel Key合并。
10. 普通 OAuth/API Key只有在管理员确认成本合同后进入 Class2/Class3；其他普通用户保持原类型。
11. 先 backfill、唯一约束审计和 shadow-read，再比较旧/新 model解析与经济快照；切换使用按 Channel feature flag，
    保留 rollback read path。回滚不删除新账本、远端实例历史或已经产生的负余额事实。
12. 迁移不得启动 Provider shadow timer，不得改变未关联普通用户和账号。

## 16. 验收

至少覆盖：

- 数据库唯一约束、Decimal/向上量化和凭据加密测试。
- 两部门权限边界和两种 Key 独立轮换 Saga，包括每个中断点。
- 通用 A6 控制台对受管资源写入必须进入 Outbox/single writer，以及 incomplete分页快照不能证明资源不存在的测试。
- API Key 固定 UserID、requested model 唯一解析 Listing、未知/歧义模型在调度前短路测试。
- 一输出 Channel 一用户一入口 Key、多 Listing 隔离测试：同一 Key 的不同模型命中不同 Listing 经济事实，
  但不切换 UserID；一个 Listing 的 delta/状态不得串到同 Channel 其他模型。
- 稳定 binding/远端 instance 测试：远端 Listing/Channel 删除重建后 override、attempt、hold、账单和历史实例不断链。
- platform model精确规范化、显式 alias、normalization generation和碰撞进入 BINDING_REVIEW测试。
- `users.status` 不随单 Listing状态变化、同 Key `/v1/models` 不泄漏其他 Channel模型、逐模型 admission测试。
- Managed Seller 单 Channel原地迁移、同一 Channel多个旧用户合并和一个旧用户多 Channel拆分测试；验证 anchor
  证据、原 `users.id`/Key、非 anchor retirement、历史账本保留，以及全部 Listing无遗漏/重复。
- `concurrency=0`、`quota=0` 无限语义测试；余额 0 首次请求、连续负余额、auth/BillingCache 不按余额拒绝、
  原子扣费和缓存负数一致性测试；确认巨额余额触发器已移除。
- Class2 multiplier默认 1.0、utilization 0%、6%、100%、>100%、字段矛盾只改变 sk/估算状态而不改变调度的测试。
- Class1 Ban read-before-write、maintenance listing、Auto-Pool最坏带成本、Pin精确成本/repin、ROUTE_UNKNOWN 和
  Token轮换/DELETE双读回测试。
- per-token input=0、output-only、per-request、未知 billing、commission漂移、8 个对数桶、公开 k转净 ck、最大余数、
  零样本、稳定匹配、硬上下限和批量限速测试。
- `(listing_binding, account, model)` delta_auto/override、failover换快照、dispatch后 generation冻结、winner-only
  billing、同 snapshot不同 usage hash拒绝二次结算、通用 Adjustment防双加成测试。
- Delta/ActualCost/负余额/A6 收益映射端到端测试：以 request_id 对齐 A6 earnings，保存原始/调整 usage、
  `quota_micros/earned_micros`、commission、价格/FX generation、本地 ActualCost、余额变化、延迟对账和 dedup证据；
  明确验证本地账本与 A6 micros不做错误的绝对值比较。
- ck 下调在途 drain、超时取消、PUT结果未知采用/重放/第三值和 ck上调顺序测试。
- 首选、粘性、failover、retry、Replay、抢槽重检全部经过同一硬门的调度矩阵。
- 结构性 NO_ELIGIBLE、已派发 attempt全部失败、纯 busy三个互斥终态和普通 503响应测试。
- `COOLING`、`REMOTE_HOLD`、`REMOTE_COOLING`、`REMOTE_UNKNOWN`、`REMOTE_UNCALLABLE`、NO_ELIGIBLE 和绑定审核的互斥谓词测试。
- Supplier Listing `status/visibility/availability/down_reason` 映射、circuit channel/model scope 扇出、派生
  cooling epoch 和 callable 证据测试。
- 多 hold 并存及按 owner 独立清除、本地 hold 抢占远端恢复、旧 generation supersede、同一 Listing 单写者和
  “hold 集合清空后再选择远端恢复”测试。
- UNLISTED direct preflight -> PROBE_ONLY relist -> A6 consumer Probe -> earnings -> OPEN闭环，以及普通流量/旧 HMAC
  在 PROBE_ONLY被拒测试。
- circuit `scope=channel` 单父 operation、scope=model单 Listing operation、锁顺序、canary分批恢复和子 Listing
  不重复 reset测试。
- Channel/Listing创建、替换、单 Listing退役和最后一个 Listing退役 Saga各中断点测试。
- errorlist分类、incident fingerprint、hold owner清除、canary分批恢复和防误扩散测试。
- attempt/request/circuit事件去重、轮询不重复 Outbox、5 分钟 drift sweep补漏测试。
- Outbox 乱序、多实例租约、写结果不明、精确读回、dead-letter 和人工重放测试。
- 旧定制能力及生产升级回归测试。

## 17. 历史协议注记

- HAR确认已有Supplier也使用 `POST /api/supplier/apply` 申请第二个Channel；申请获批并在channels读回新ID后完成。
- 抓包没有独立的“按原ID重新启用已takedown Channel”动作；Channel replacement直接申请新Channel并切换instance，
  因而不依赖原ID复活。
- per-request、Listing恢复判定、usage舍入和1000 input门限均以当前精简Prompt为准，本节不再保存开放实现缺口。

实现完成的判定不是“接口返回成功”，而是两部门资源边界明确、Channel/Listing/账号三种颗粒度没有串账，
每个远端写入都有精确 readback，每个暂停状态都有恢复或人工出口，所有请求路径共享同一 eligibility generation，
Delta 在本地 ActualCost 和 A6 实际收益中使用同一 winner 快照，并且迁移可回滚。
