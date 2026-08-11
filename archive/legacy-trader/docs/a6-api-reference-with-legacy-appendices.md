# A6 API 接口总览

> 基于当前仓库运行代码、已保存的前端协议证据和只读/临时资源实测整理。核对日期：2026-08-11。

## 1. 文档口径

本文覆盖三类接口：

1. A6 控制面：`https://a6api.com`，负责账号、Token、供应商、Listing、财务和市场路由。
2. A6 OpenAI 兼容请求面：`https://a6.a6api.com`，负责模型查询与模型请求。
3. 本项目自己的 Dashboard 和 Provider relay：放在附录中，避免与 A6 外部接口混淆。

当前源码确认的 A6 接口共有 **46 个 HTTP 方法与路径组合**：

- 控制面 43 个，均由 `src/a6api_auto/client.py` 封装。
- 请求面 3 个，由 `src/a6api_auto/client.py` 或 `src/a6api_auto/trade_upstream_relay.py` 调用。
- 另有一组控制面接口只存在于已保存的前端协议证据或临时资源实测中，当前客户端尚未全部封装；本文仍将其
  作为已观测接口列出，不用一个容易失真的固定数量掩盖差异。

状态说明：

| 状态 | 含义 |
| --- | --- |
| 当前封装 | 当前 `A6ApiClient` 有对应方法和测试，属于主清单 |
| 当前探测 | 当前运行代码直接请求，但没有独立业务封装 |
| 已观测未封装 | 历史前端资产中已捕获，当前代码不依赖 |
| 设计记录 | 设计文档提到，但当前仓库没有 A6 上游调用证据，不计入 46 个已确认接口 |

主要证据：

- `src/a6api_auto/client.py`
- `src/a6api_auto/trade_upstream_relay.py`
- `tests/test_client.py`
- `docs/protocol.md`
- `docs/a6-trading-system-design.md`
- `api-docs/api-supplier-profile.md`
- `api-docs/api-status.md`

`api.a6api.com`、`a6.a6api.com` 与 `a6api.com` 是不同的请求边界。本文不把它们的响应或可用性结论混算。

## 2. 服务与鉴权

| 服务 | Base URL | 鉴权 | 主要用途 |
| --- | --- | --- | --- |
| A6 控制面 | `https://a6api.com` | 统一凭据对 `(a6_user_id, system_access_token)` | 账号、Token、供应商、财务、市场 |
| A6 请求面 | `https://a6.a6api.com` | `Authorization: Bearer API_KEY` | OpenAI 兼容模型请求 |
| 本地 Provider relay | 默认 `http://127.0.0.1:8767` | 原样转发调用方 Bearer Token | 固定直连 A6 请求面 |
| 本地 Trader Dashboard | 默认 `http://127.0.0.1:8765` | 外围访问控制；部分内部路由另用 Bearer Token | 经营读 API 与控制操作 |

### 2.1 统一控制面鉴权对

自本版起，所有需要认证的 A6 控制面请求统一使用不可拆分的凭据对：

```text
(a6_user_id, system_access_token)
```

本文中的“系统管理令牌”与接口返回的“系统访问令牌”指同一种凭据，持久化字段统一命名为 `system_access_token`。两个字段固定映射到以下请求头：

```http
Authorization: Bearer SYSTEM_ACCESS_TOKEN
New-API-User: A6_USER_ID
Accept: application/json, text/plain, */*
```

Shell 变量约定：

```bash
export A6_USER_ID='A6_USER_ID'
export A6_SYSTEM_ACCESS_TOKEN='SYSTEM_ACCESS_TOKEN'
```

通用控制面调用模板：

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${A6_SYSTEM_ACCESS_TOKEN}" \
  -H "New-API-User: ${A6_USER_ID}" \
  -H 'Accept: application/json, text/plain, */*' \
  'https://a6api.com/api/ENDPOINT'
```

必须遵守以下规则：

- 每个认证请求同时发送两个请求头，不单独使用其中任意一个字段。
- `A6_USER_ID` 必须等于该 `SYSTEM_ACCESS_TOKEN` 所属用户的 A6 用户 ID，禁止跨用户混配。
- `system_access_token` 是 opaque token，必须按接口返回值原样使用；即使值没有 `sk-` 前缀，也不得自行添加 `sk-`。
- 不从 `system_access_token` 推导用户 ID。`a6_user_id` 取自登录响应 `data.id` 或受控凭据文件。
- 轮换系统管理令牌时，原子替换同一 `a6_user_id` 对应的 `system_access_token`，后续请求立即使用新凭据对。
- Session Cookie 不作为日常控制面鉴权方式。验证码、注册、登录以及生成或轮换系统管理令牌属于引导流程例外；注册、验证码和登录使用 `New-API-User: -1`，`GET /api/user/token` 使用已登录 Session 和真实 `A6_USER_ID`。

只携带 Bearer 令牌而缺少 `New-API-User` 时，当前线上服务返回 `401 Unauthorized, New-Api-User header not provided`。

### 2.2 控制面响应信封

控制面通常返回：

```json
{
  "success": true,
  "message": "",
  "data": {}
}
```

错误可能出现在顶层 `message`、`error`，或嵌套的 `code` / `error_code` 中。调用方必须同时检查 HTTP 状态、`success` 和 `data` 类型。

分页响应通常为：

```json
{
  "success": true,
  "data": {
    "items": [],
    "total": 0,
    "page": 1,
    "size": 100
  }
}
```

不同接口可能使用 `size` 或 `page_size`。`supplier/listings` 还兼容 `data` 直接为数组的旧响应。

### 2.3 重试与写后读

- 当前客户端对 GET 的传输错误最多尝试 2 次，并共享同一个总超时预算。
- `GET /api/user/token` 具有生成或轮换凭据的写入语义，不得纳入通用 GET 自动重试。响应丢失时，应先验证原令牌状态，再决定是否重新生成。
- POST、PUT、PATCH 不做传输层自动重试，避免重复写。
- A6 控制面写接口没有在当前客户端暴露统一 Idempotency Key。响应丢失时，应通过对应 GET 接口读回，不应盲目重复提交。
- 所有凭据、Cookie、完整 API Key 和提现地址都不得进入日志或文档样例。

### 2.4 金额、Quota 与倍率

- `*_micros`、`quota`、`amount` 均按 A6 返回的整数原值保存。
- 金额与用户 Quota 的换算由每个 Session 的 `quota_per_unit` 决定。当前默认值是 `500000`，即当前运行口径下 `500000 quota = 1 USD`。
- 提现客户端约束 `amount >= 50000000`。
- 余额划转客户端约束 `amount >= quota_per_unit`。
- `min_ratio`、`max_ratio` 使用十进制定点字符串，避免二进制浮点误差。

## 3. A6 请求面接口

Base URL：`https://a6.a6api.com`

| 方法 | 路径 | 状态 | 用途 |
| --- | --- | --- | --- |
| GET | `/v1/models` | 当前探测 | 模型列表；relay 就绪探测也用无凭据 `401` 验证完整链路 |
| POST | `/v1/responses` | 当前封装 | Responses API，支持非流式与 SSE |
| POST | `/v1/chat/completions` | 当前封装 | Chat Completions API，当前探测为非流式 |

### 3.1 `GET /v1/models`

请求：

```http
GET /v1/models
Authorization: Bearer API_KEY
Accept: application/json
```

正常响应遵循 OpenAI 兼容模型列表，核心字段为 `data[].id`。Provider relay 的 readiness 不带认证，请求得到完整 `401` 即视为 A6 网络、TLS 和 HTTP 链路可达；这个 `401` 不是业务可调用证明。

### 3.2 `POST /v1/responses`

普通非流式请求体：

```json
{
  "model": "MODEL",
  "input": "INPUT_TEXT",
  "max_output_tokens": 128,
  "reasoning": {"effort": "low"},
  "stream": false
}
```

流式探测请求体：

```json
{
  "model": "MODEL",
  "input": "INPUT_TEXT",
  "max_output_tokens": 16,
  "reasoning": {"effort": "low"},
  "stream": true
}
```

普通非流式请求头：

```http
Authorization: Bearer API_KEY
Content-Type: application/json
Accept: application/json
```

流式探测将 `Accept` 改为 `text/event-stream`，并额外发送
`X-Request-ID: REQUEST_ID`。当前探测器在收到首个合法 `response.created` 事件后即确认路由并关闭流；错误事件会提取稳定错误码。当前代码明确识别：

- `fixed_merchant_unavailable`
- `insufficient_quota`
- `account_quota_insufficient`
- `no_available_channel`

生产流动性/回调请求使用以下精确输入，并验证现实用量及高缓存特征：

```text
PING 5db98307ce2d442ee568baacf8b0e7076fa657cbe2806ccff9da6386997915e2
```

`prompt_tokens=1`、`completion_tokens=0`、`total_tokens=1` 这类探测形状不计为有效生产转移。

### 3.3 `POST /v1/chat/completions`

当前探测请求体：

```json
{
  "model": "MODEL",
  "messages": [
    {"role": "user", "content": "INPUT_TEXT"}
  ],
  "max_completion_tokens": 16,
  "stream": false
}
```

鉴权与 Responses 相同。当前仓库只直接实现非流式探测；设计文档同时记录了流式 `chunk + [DONE]` 的协议适配目标。

### 3.4 仅存在于设计记录的路径

以下路径出现在 Sub2API replay/adapter 设计记录中，但当前 Python 源码没有直接调用 A6 上游的证据，因此不计入已确认清单：

- `/v1/messages`
- `/v1/responses/compact`
- Responses WebSocket Upgrade

## 4. 账号与会话

第 4 至第 10 节的所有 A6 控制面端点，除验证码、注册、登录、状态查询和 `GET /api/user/token` 等明确标注的引导或免认证流程外，均继承第 2.1 节的统一鉴权对，并同时发送：

```http
Authorization: Bearer SYSTEM_ACCESS_TOKEN
New-API-User: A6_USER_ID
```

后续各接口表格不再重复列出这两个必需请求头。

| 方法 | 路径 | Query | JSON Body | 状态 |
| --- | --- | --- | --- | --- |
| GET | `/api/status` | 无 | 无 | 响应已捕获，未封装 |
| GET | `/api/verification` | `email`, `turnstile=` | 无 | 当前封装 |
| POST | `/api/user/register` | `turnstile=` | 注册资料 | 当前封装 |
| POST | `/api/user/login` | `turnstile=` | 登录资料 | 当前封装 |
| GET | `/api/user/self` | 无 | 无 | 当前封装 |
| GET | `/api/user/token` | 无 | 无 | 已观测未封装 |

`GET /api/status` 的完整实测响应保存在 `api-docs/api-status.md`。`data` 是平台动态配置对象；Typed Client应强类型化本模块
实际使用的 `quota_per_unit`、`payment_fx_rate_cny_per_usd`、`price`、`start_time`、`system_name` 等字段，同时保留未知字段，
避免平台新增配置导致反序列化失败。

### 4.1 发送邮箱验证码

```http
GET /api/verification?email=EMAIL&turnstile=
New-API-User: -1
```

成功要求 HTTP `200` 且 `success=true`。若返回消息包含邮箱已被占用，当前客户端归类为 `email_already_registered`。

### 4.2 注册

```json
{
  "username": "USERNAME",
  "password": "PASSWORD",
  "password2": "PASSWORD",
  "email": "EMAIL",
  "verification_code": "OTP_CODE",
  "wechat_verification_code": "",
  "aff_code": null
}
```

### 4.3 登录

```json
{
  "username": "USERNAME_OR_EMAIL",
  "password": "PASSWORD"
}
```

成功时 `data` 是用户对象，并由响应写入 Cookie。当前恢复流程先试用户名，再试邮箱。

### 4.4 当前用户

`GET /api/user/self` 用于校验 `(a6_user_id, system_access_token)` 的精确账号绑定；引导流程中也可用于校验 Session Cookie。项目已确认的核心财务字段包括：

| 字段 | 含义 |
| --- | --- |
| `data.id` | A6 用户 ID |
| `data.quota` | 当前用户余额 |
| `data.used_quota` | 累计消费 |
| `data.request_count` | 累计调用数 |

### 4.5 生成系统访问令牌

`GET /api/user/token` 使用已登录的 Session 生成或刷新当前用户的系统访问令牌。尽管方法是 `GET`，该接口具有凭据变更语义，调用方应将每次请求视为一次可能的令牌轮换。

请求：

```bash
curl --fail-with-body -sS \
  -b 'session=SESSION_COOKIE' \
  -H 'Accept: application/json, text/plain, */*' \
  -H 'New-API-User: A6_USER_ID' \
  -H 'Referer: https://a6api.com/console/personal' \
  'https://a6api.com/api/user/token'
```

`accept-language`、`sec-ch-ua*`、`sec-fetch-*`、`priority` 和 `user-agent` 是浏览器附带的请求头，不是当前已观测协议的必需字段。

成功响应：

```json
{
  "data": "SYSTEM_ACCESS_TOKEN",
  "message": "",
  "success": true
}
```

返回的 `data` 是完整凭据，不是 Token ID。后续控制面请求示例：

```bash
curl --fail-with-body -sS \
  -H "Authorization: Bearer ${A6_SYSTEM_ACCESS_TOKEN}" \
  -H "New-API-User: ${A6_USER_ID}" \
  -H 'Accept: application/json' \
  'https://a6api.com/api/user/self'
```

生成成功后必须持久化完整凭据对，而不是只保存令牌：

```json
{
  "a6_user_id": 12345,
  "system_access_token": "SYSTEM_ACCESS_TOKEN"
}
```

当前批量凭据文件为：

```text
/var/lib/a6-trader/secrets/a6-system-access-tokens.json
```

账号记录位于 `accounts[]`，日常请求只读取其中配对的 `a6_user_id` 与 `system_access_token`。文件必须保持 `0600`，完整令牌不得进入日志。

该凭据与 `/api/token/` 创建的模型调用 API Key 属于两个鉴权边界：

- `SYSTEM_ACCESS_TOKEN` 用于 `https://a6api.com/api/*` 控制面。
- `API_KEY` 用于 `https://api.a6api.com/v1/*` 或 `https://a6.a6api.com/v1/*` 请求面。

## 5. Token / API Key

| 方法 | 路径 | Query / Body | 用途 |
| --- | --- | --- | --- |
| GET | `/api/token/` | `p`, `size` | Token 分页列表 |
| GET | `/api/token/{token_id}` | 无 | 单个 Token 读回 |
| POST | `/api/token/` | Token 元数据 | 创建 Token |
| PUT | `/api/token/` | 含 `id` 的完整元数据 | 更新 Token |
| POST | `/api/token/{token_id}/key` | 无 Body | 显示完整 Key |
| DELETE | `/api/token/{token_id}` | 无 | 删除 Token |

### 5.1 列表与详情

当前列表固定使用：

```http
GET /api/token/?p=1&size=100
```

详情接口用于更新前读取完整元数据，也用于写后确认 Token ID 和名称。

### 5.2 创建 Token

当前创建体：

```json
{
  "name": "TOKEN_NAME",
  "remain_quota": 0,
  "expired_time": -1,
  "unlimited_quota": true,
  "model_limits_enabled": false,
  "model_limits": "",
  "allow_ips": "",
  "group": "default",
  "cross_group_retry": false
}
```

`name` 必须从 `src/a6api_auto/data/token_name_corpus.json` 的 `names[]` 选择；创建、轮换和重命名均保存语料文件
SHA-256、数组索引、选中值与 selection generation，重试复用同一选择，重名时改选另一项。

### 5.3 更新 Token

当前客户端允许更新：

```text
id, name, remain_quota, remain_amount, expired_time, unlimited_quota,
model_limits_enabled, model_limits, allow_ips, group, cross_group_retry,
routing_enabled, routing_preference, pricing_strategy, pricing_custom_pct,
routing_allow_fallback
```

重命名流程是 `GET detail -> PUT merged metadata -> GET detail`，避免覆盖服务端已有路由设置。完整 `key` 字段不会放回更新请求。

### 5.4 显示完整 Key

```http
POST /api/token/TOKEN_ID/key
```

成功数据形状：

```json
{
  "success": true,
  "data": {"key": "KEY_MATERIAL"}
}
```

调用方统一将结果规范为 `sk-...`。该接口返回敏感数据，只应在内存和权限受控的 secret 文件中处理。

### 5.5 删除 Token

```http
DELETE /api/token/TOKEN_ID
Authorization: Bearer SYSTEM_ACCESS_TOKEN
New-API-User: A6_USER_ID
Accept: application/json
```

2026-08-11 使用临时 Token 实测确认该接口支持统一控制面凭据
`(a6_user_id, system_access_token)`，成功响应为：

```json
{"message":"","success":true}
```

删除是不可逆写操作。调用方必须先记录待删 Token 的 ID、用途和当前替代 Token，DELETE 后同时执行：

1. `GET /api/token/{token_id}`，期望 `success=false` 且 `message="record not found"`。
2. `GET /api/token/?p=1&size=...`，期望列表中不存在该 ID。

HTTP 200 本身不代表删除完成，必须检查 JSON `success` 并完成上述双读回。当前
`A6ApiClient` 尚未封装该方法，Typed Client 实现时应新增 `delete_token`。

## 6. 供应商申请与渠道

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/supplier/fetch-models` | 由 A6 代查上游模型 |
| POST | `/api/supplier/apply` | 首次申请 Supplier，或为已有 Supplier 申请新增 Channel |
| GET | `/api/supplier/me/requests` | 查询申请记录 |
| GET | `/api/supplier/me` | 查询当前供应商资料 |
| PATCH | `/api/supplier/me/profile` | 更新供应商联系方式和简介，请求已确认、未封装 |
| GET | `/api/supplier/me/channels` | 查询当前渠道 |
| PATCH | `/api/supplier/me/channels/{channel_binding_id}` | 更新渠道 |
| POST | `/api/supplier/channels/{channel_binding_id}/takedown` | 下架渠道 |
| GET | `/api/supplier/channels/markup-preview` | 批量查询渠道加价来源和百分比 |

`PATCH /api/supplier/me/profile` 的实测请求体为：

```json
{
  "contact_info": "CONTACT_INFO",
  "description": "SUPPLIER_DESCRIPTION"
}
```

成功响应为`{"message":"","success":true}`，原始抓包见`api-docs/api-supplier-profile.md`。当前抓包请求体没有`name`字段，
因此只确认联系方式和简介更新，尚不能据此推导Supplier重命名能力；写后读取`GET /api/supplier/me`核对。

### 6.1 A6 代查上游模型

```json
{
  "base_url": "UPSTREAM_BASE_URL",
  "key": "UPSTREAM_API_KEY",
  "channel_type": 1
}
```

成功时 `data` 是模型字符串或含 `id` 的对象数组。项目优先直接查询上游 `/models` 或 `/v1/models`，两者都失败后才调用此接口。

### 6.2 Supplier 与新增 Channel 申请

```json
{
  "supplier": {
    "name": "SUPPLIER_NAME",
    "contact_info": "CONTACT_EMAIL",
    "description": "SUPPLIER_DESCRIPTION"
  },
  "channel": {
    "name": "CHANNEL_NAME",
    "nickname": "CHANNEL_NICKNAME",
    "channel_type": 1,
    "base_url": "UPSTREAM_BASE_URL",
    "api_key": "UPSTREAM_API_KEY",
    "models": "MODEL_A,MODEL_B",
    "description": "CHANNEL_DESCRIPTION"
  }
}
```

`models` 是逗号分隔字符串，不是 JSON 数组。若返回稳定错误码 `pending_request_exists`，表示已有待处理申请，应读 `GET /api/supplier/me/requests` 对账。

公共文本必须先从版本化仓库语料选择：Supplier `name/description` 来自
`src/a6api_auto/data/merchant_templates_split.xlsx` 的“商户资料”同一行；Channel `name/description` 与Listing
`description` 来自“渠道资料”同一行的 `channel_name/channel_description/model_description`；所有Token/Key `name`
来自 `src/a6api_auto/data/token_name_corpus.json`。`nickname`只投影所选Channel名以适配平台长度。请求必须记录语料文件
SHA-256、sheet、source row/index和selection generation；重名时改选未占用项，不追加ID、模型、职责、时间或后缀。
Listing `display_name`固定为空，直至版本化语料新增专用字段。语料校验失败时不发送远端写入，管理端只提供语料选择和随机重选。

该端点同时覆盖两个场景：

1. 尚未成为 Supplier：提交 Supplier资料和第一个 Channel。
2. 已经是 Supplier：保留当前 Supplier资料并提交一个新的 Channel申请。

HAR已确认第二种场景：同一账号在 `/api/supplier/me` 返回200且已有Channel时，从 `/supplier/channels` 页面再次
调用该端点；重名请求返回409，更换Channel名称后返回200，随后 `/api/supplier/me/requests` 响应增大。因此新增
Channel使用同一申请端点，成功只表示申请已提交；控制器继续读取requests和channels，直到新Channel获批并出现。

### 6.3 申请、供应商和渠道读回

- `GET /api/supplier/me/requests`：`data` 为申请数组。
- `GET /api/supplier/me`：`data` 为供应商对象；供应商尚未生效时可能返回业务型 `403`。
- `GET /api/supplier/me/channels`：`data` 为渠道数组。当前客户端兼容旧的扁平结构和新的 binding/channel/request 嵌套结构。

渠道读回中需要区分：

- `id`：A6 Channel ID。
- `binding_id`：供应商渠道绑定 ID，渠道更新与下架路径通常使用它。

### 6.4 更新与下架渠道

当前项目写入过的更新字段：

```json
{
  "nickname": "CHANNEL_NICKNAME",
  "description": "CHANNEL_DESCRIPTION",
  "models": "MODEL_A,MODEL_B"
}
```

字段可按需提交。下架请求发送空对象：

```http
POST /api/supplier/channels/CHANNEL_BINDING_ID/takedown
Content-Type: application/json

{}
```

现有抓包只确认 Channel 级 takedown，尚未观测同一 `channel_binding_id` 的 `resubmit`、`restore` 或 `enable` 请求。
`POST /api/supplier/listings/{listing_id}/resubmit` 只恢复单个 Listing，不恢复整个 Channel。需要替换已下架 Channel 时，
当前合同是通过 `POST /api/supplier/apply` 申请新 Channel，获批读回新 ID 后切换稳定本地 binding。

### 6.5 渠道加价预览

```http
GET /api/supplier/channels/markup-preview?channel_ids=CHANNEL_ID_A,CHANNEL_ID_B
```

`channel_ids` 是逗号分隔的 A6 Channel ID。2026-08-11 实测响应：

```json
{
  "data": {
    "items": [
      {
        "channel_id": 123,
        "percent": 20,
        "source": "supplier"
      }
    ]
  },
  "message": "",
  "success": true
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `data.items[].channel_id` | integer | 被查询的 A6 Channel ID |
| `data.items[].percent` | integer/number | 当前公开加价百分比；`20` 对应 `commission=0.2` |
| `data.items[].source` | string | 加价配置来源；当前实测值为 `supplier` |

响应项顺序不保证与请求顺序一致，调用方必须按 `channel_id` 建立映射。每个受管 Channel 都应恰好返回一项；缺项、
重复项、非数值 percent 或 `percent != 20` 均进入输出配置校验。保存 `source` 原始值用于审计，不根据数组位置绑定。

该接口用于 Supplier Listing 页面预览渠道的平台加价/公开价格关系，与提现无关：

```text
commission = Decimal(percent) / 100
ck_public = ck * (1 + commission)
```

## 7. Listing

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/supplier/listings` | 分页查询 Listing |
| POST | `/api/supplier/listings` | 批量创建 Listing |
| PUT | `/api/supplier/listings/{listing_id}` | 更新 Listing |
| POST | `/api/supplier/listings/{listing_id}/takedown` | 下架 Listing |
| POST | `/api/supplier/listings/{listing_id}/resubmit` | 重新提交 Listing |

### 7.1 查询

```http
GET /api/supplier/listings?page=1&size=1000&status=STATUS
```

`status` 可省略。当前客户端将结果规范为：

```json
{
  "items": [],
  "total": 0,
  "page": 1,
  "size": 1000
}
```

2026-08-11 实测的 Listing 状态事实字段包括：

```text
status, visibility, availability, down_reason,
channel_id, supplier_channel_id, own_model, platform_model
```

当前观测到的组合语义为：

| 状态 | `status` | `visibility` | `availability` | `down_reason` |
| --- | ---: | ---: | ---: | --- |
| 商户上架且控制面可用 | 2 | 1 | 1 | 空字符串 |
| 商户下架 | 0 | 0 | 0 | `商户下架` |

这些是当前运行时观测值，业务代码应保存原始枚举和观测时间，不应只保存一个本地布尔值。

### 7.2 创建

请求体必须是数组，即使只创建一个 Listing：

```json
[
  {
    "channel_id": 123,
    "own_model": "MODEL",
    "platform_model": "MODEL",
    "charge_type": "per_token",
    "input_price_micros": 500000,
    "output_price_micros": 3000000,
    "cache_read_price_micros": 50000,
    "cache_write_price_micros": 625000,
    "display_name": "",
    "description": "DESCRIPTION"
  }
]
```

当前Supplier Listing抓包只确认`charge_type="per_token"`及四个token价格字段。本项目按该合同创建和更新Listing。

### 7.3 更新

当前项目的更新体包含：

```json
{
  "charge_type": "per_token",
  "input_price_micros": 500000,
  "output_price_micros": 3000000,
  "cache_read_price_micros": 50000,
  "cache_write_price_micros": 625000,
  "display_name": "",
  "description": "DESCRIPTION"
}
```

创建和更新后都按Listing ID读回`charge_type="per_token"`与完整token价格向量。A6商户结算使用返回token usage；
delta沿用usage改写链路，不引入固定按次结算分支。

更新、下架和重新提交后都应重新分页读取 Listing，以远端状态为准。两个动作接口都发送 `{}`。

### 7.4 平台模型官方价格目录

```http
GET /api/platform-models?enabled=1&size=1000
Authorization: Bearer SYSTEM_ACCESS_TOKEN
New-API-User: A6_USER_ID
Accept: application/json
```

该目录是 Listing `K_CNY` 计算的官方价格向量来源。已确认字段包括：

```text
id, brand, model_no, billing, enabled, sellers, official_source,
official_input_price_micros, official_output_price_micros,
official_cache_read_price_micros, official_cache_write_price_micros,
official_per_request_price_micros, created_at, updated_at
```

`official_per_request_price_micros`是平台目录的真实返回字段，但本项目不把它映射为Supplier Listing写入字段，也不用于ck或delta。

调用方必须按 `model_no` 精确匹配，保存完整官方向量、`official_source`、目录版本/hash 和抓取时间。以 input
维度计算商户净倍率：

```text
fx_usd_cny = realtime_ratio_cny / realtime_ratio_usd
ck = fx_usd_cny * listing.input_price_micros / official_input_price_micros
```

output/cache_read/cache_write 维度应使用各自官方价格做交叉校验；不能用 input 官方价解释其他维度。

## 8. 收益、结算、提现与余额划转

### 8.1 接口总表

| 方法 | 路径 | Query / Body | 用途 |
| --- | --- | --- | --- |
| GET | `/api/supplier/earnings/summary` | 无 | 收益汇总 |
| GET | `/api/supplier/earnings` | `page`, `size` | 收益明细 |
| GET | `/api/supplier/settlements/summary` | 无 | 结算汇总 |
| GET | `/api/supplier/settlements` | `page`, `size` | 结算明细 |
| GET | `/api/supplier/withdraw/summary` | 无 | 可提现与提现中余额 |
| GET | `/api/supplier/withdraw/preview` | `account_id`, `amount` | 提现预览 |
| GET | `/api/supplier/withdraw` | `page`, `size` | 提现记录 |
| POST | `/api/supplier/withdraw` | `account_id`, `amount` | 提交提现 |
| GET | `/api/supplier/withdraw/accounts` | 无 | 提现账户列表 |
| POST | `/api/supplier/withdraw/accounts` | 提现账户资料 | 创建提现账户 |
| GET | `/api/supplier/balance/transfer/preview` | `amount` | 商户余额转用户余额预览 |
| POST | `/api/supplier/balance/transfer` | `amount` | 执行余额划转 |

### 8.2 已确认的汇总字段

`GET /api/supplier/earnings/summary`：

- `today_earned_micros`
- `month_earned_micros`
- `today_calls`
- `month_calls`

`GET /api/supplier/withdraw/summary`：

- `balance_available_micros`
- `withdrawing_micros`
- `total_earned_micros`
- `total_paid_micros`

收益明细已确认包含请求、渠道、模型、输入/输出/缓存 Token、`quota_micros`、`earned_micros`、倍率、结果和耗时等字段。

### 8.3 提现预览与提交

预览：

```http
GET /api/supplier/withdraw/preview?account_id=ACCOUNT_ID&amount=AMOUNT_MICROS
```

提交：

```json
{
  "account_id": 132,
  "amount": 55000000
}
```

HAR 已完整保存提交路径、`application/json` 和两个整数请求字段；该次请求以
`net::ERR_INTERNET_DISCONNECTED` 结束，因此没有成功响应、提现单 ID或余额变化样本。

当前客户端要求 `account_id > 0` 且 `amount >= 50000000`。提交后通过 `GET /api/supplier/withdraw?page=1&size=1000` 按 `id` 或 `withdrawal_id` 对账。

### 8.4 创建 USDT 提现账户

外层请求：

```json
{
  "channel": "usdt",
  "remark": "STABLE_REMARK",
  "account_info": "SERIALIZED_JSON_STRING",
  "is_default": true
}
```

`account_info` 是 JSON 字符串，不是嵌套对象。反序列化后的结构为：

```json
{
  "channel": "usdt",
  "remark": "STABLE_REMARK",
  "display": "NETWORK: MASKED_ADDRESS",
  "fields": {
    "network": "NETWORK",
    "address": "FULL_ADDRESS"
  }
}
```

完整地址属于敏感数据。创建响应丢失时，使用稳定 `remark` 和地址指纹读回对账。

### 8.5 商户余额转用户余额

预览：

```http
GET /api/supplier/balance/transfer/preview?amount=500000
```

已观测的成功 `data` 示例：

```json
{
  "amount_micros": 500000,
  "quota_to_add": 500000,
  "fee_micros": 0,
  "net_micros": 500000,
  "balance_available_micros": 3267676,
  "enabled": true,
  "rate_limit_seconds": 3
}
```

执行：

```json
{
  "amount": 500000
}
```

调用方每次先读 Preview，并以服务端返回的 `enabled`、手续费、限频和可用余额为准。

## 9. 用户调用日志

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/log/self` | 查询当前用户的请求与用量明细 |

当前查询参数：

```text
p=PAGE
page_size=PAGE_SIZE
type=0
token_name=TOKEN_NAME
model_name=MODEL_NAME
start_timestamp=UNIX_SECONDS
end_timestamp=UNIX_SECONDS
group=
request_id=REQUEST_ID
```

除时间和分页外，过滤字段可为空。已确认的明细字段包括：

- `token_id`, `token_name`
- `channel`, `channel_name`
- `model_name`
- `quota`
- 输入、输出和缓存 Token 用量
- `request_id`
- 路由类型、结果和耗时

该接口是 Provider 成本和请求归因的重要事实来源。分页同步时应按稳定事件身份去重，而不是只依赖页码。

## 10. Marketplace、订阅、Auto-Pool 与 Pin

### 10.1 接口总表

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/marketplace/hot-models` | 热门模型/品牌聚合 |
| GET | `/api/marketplace/pins` | 当前用户的 Pin 列表 |
| GET | `/api/marketplace/channels/search` | 搜索可路由渠道 |
| GET | `/api/marketplace/circuit-states` | 查询 Channel/模型熔断与 cooling 状态 |
| GET | `/api/marketplace/platform-models/{model}/price-range` | 查询模型市场价格范围 |
| POST | `/api/marketplace/channels/{channel_id}/disable` | 对当前用户禁用渠道 |
| GET | `/api/subscriptions` | 订阅列表 |
| GET | `/api/subscriptions/{subscription_id}` | 订阅详情 |
| POST | `/api/subscriptions` | 创建订阅/Auto-Pool |
| PUT | `/api/subscriptions/{subscription_id}/auto-pool` | 更新 Auto-Pool |
| POST | `/api/marketplace/pin` | Pin 到固定渠道 |
| POST | `/api/marketplace/unpin` | 解除 Pin |

### 10.2 Pin 列表

`GET /api/marketplace/pins` 的 `data` 是数组。写 Pin 或 Unpin 后必须重新读取，并校验：

- Token ID
- Model
- Channel ID
- `fallback_to_smart_routing`

### 10.3 渠道搜索

完整参数集：

```text
token_id=TOKEN_ID
model=MODEL
route_status=all
exclude_unavailable=true|false
include_seed=true|false
include_disabled=true|false
include_user_disabled=true|false
sort=input_asc
view=list
page=PAGE
page_size=PAGE_SIZE
```

返回 `data.items` 分页，常用字段包括：

- `channel_id` 或 `id`
- `supplier_id`
- 四类价格 `*_price_micros`
- `route_status`
- `user_channel_disabled`
- `listing_availability`
- `supplier_channel_disabled`

2026-08-11 当前运行时的 marketplace search item 没有内嵌 `circuit_status` 或
`cooldown_status`；熔断与 cooling 必须从独立的 `circuit-states` 端点读取。调用方可兼容旧响应中的
内嵌字段，但不得依赖它们一定存在。

Provider 自路由隔离审计必须读取完整分页，固定使用：

```text
route_status=all
exclude_unavailable=false
include_seed=true
include_disabled=true
include_user_disabled=true
sort=input_asc
```

禁用成功的精确读回是：

```json
{
  "user_channel_disabled": true,
  "route_status": "user_disabled"
}
```

### 10.4 Circuit / cooling 状态

```http
GET /api/marketplace/circuit-states?limit=500
Authorization: Bearer SYSTEM_ACCESS_TOKEN
New-API-User: A6_USER_ID
Accept: application/json
```

上例中的 `500` 是 2026-08-11 抓包值。Sub2API A6 Channel 模块固定请求 `limit=1000`；返回正好 1000 项时产生容量
告警并等待管理员提高上限，该快照中的“未出现”不作为 circuit 已关闭的证据。

响应为 `data.items[]`。2026-08-11 实测字段：

```text
scope, channel_id, model, state, remaining_seconds,
probe_status, last_probe_at, probe_success_streak, probe_success_required,
next_probe_at, cooling_until, last_cooling_at,
last_error_kind, last_error_message, recovery_mode
```

已观测枚举：

```text
state: cooling | half_open | model_excluded
scope: channel | model
recovery_mode: natural | probe
probe_status: idle | running
```

`last_probe_at`、`next_probe_at`、`cooling_until` 和 `last_cooling_at` 是 Unix 数值时间。
端点没有返回独立的 `cooling_epoch`。控制器应把规范化后的
`(scope, channel_id, model, state, cooling_until, last_cooling_at, last_error_kind)` 做稳定 hash，
生成本地 `remote_cooling_epoch`，用于幂等去重；任何字段变化都产生新 epoch。

A6 也没有单一 `callable` 字段。对本模块而言，只要 Marketplace search 按
`listing_id/channel_id/model` 找到精确项、`listing_availability=1`，并且对应 circuit 没有
`cooling/half_open/model_excluded` 等阻断状态，即确认 Listing 到 Sub2API 面板的通路可用。Supplier Listing
`status=2, visibility=1, availability=1, down_reason=""` 只作为resubmit写后读审计，不构成第二套可用性门；后续请求失败按账号或route归因，
Listing恢复不再增加数据面Probe、Pin或earnings前置确认。

### 10.5 禁用渠道

```http
POST /api/marketplace/channels/CHANNEL_ID/disable?model=MODEL
```

该写操作没有 JSON Body。Provider Token 在启用 Auto-Pool 或进入可调度状态前，必须先禁用所有 A6 自有 Channel，并逐项读回确认。

### 10.6 创建 Auto-Pool

```json
{
  "token_id": 20406,
  "model_name": "MODEL",
  "channels": [],
  "caps": {
    "input_price_micros": 0,
    "output_price_micros": 0,
    "cache_read_price_micros": 0,
    "cache_write_price_micros": 0
  },
  "sort_strategy": "price_asc",
  "fallback_enabled": false,
  "auto_pool_enabled": true,
  "min_ratio": "0",
  "max_ratio": "0.0567353452"
}
```

### 10.7 更新 Auto-Pool

```json
{
  "enabled": true,
  "min_ratio": "0",
  "max_ratio": "0.0567353452"
}
```

客户端语法约束是 `min_ratio >= 0`、`max_ratio > 0`、`max_ratio >= min_ratio`。本项目生产控制合同另要求 `max_ratio <= 0.3`，且任何更新前都要重新执行完整 owned-Channel Ban 审计。

创建或更新后读取 `GET /api/subscriptions/{subscription_id}`。有效 Auto-Pool 订阅还必须满足：

- `auto_channels` 非空。
- `auto_channels` 不含任何 A6 自有 Channel。
- Token、Model、倍率与预期一致。

### 10.8 Pin 与 Unpin

Pin：

```json
{
  "token_id": 14020,
  "channel_id": 839,
  "model_name": "MODEL",
  "fallback_to_smart_routing": false
}
```

Unpin：

```json
{
  "token_id": 14020,
  "model_name": "MODEL"
}
```

### 10.9 热门模型与价格范围

```http
GET /api/marketplace/hot-models?limit=LIMIT&brand=OPTIONAL_BRAND
GET /api/marketplace/platform-models/MODEL/price-range
```

`hot-models` 用于模型/品牌聚合展示；`brand` 可省略。`price-range` 的模型名是路径参数，调用方必须做 URL
编码。两者是市场观测接口，不能替代 `/api/platform-models` 的官方价格向量，也不能替代完整分页
`channels/search` 的路由或 owned-Channel Ban 审计。

## 11. 调用示例

以下示例只展示协议结构，所有值均为占位符。

### 11.1 加载统一鉴权对

```bash
export A6_USER_ID='A6_USER_ID'
export A6_SYSTEM_ACCESS_TOKEN='SYSTEM_ACCESS_TOKEN'

test -n "${A6_USER_ID}" \
  && test -n "${A6_SYSTEM_ACCESS_TOKEN}"
```

也可以按商户从持久化文件读取配对值：

```bash
A6_MERCHANT_ID='MERCHANT_ID'
A6_CREDENTIAL_FILE='/var/lib/a6-trader/secrets/a6-system-access-tokens.json'

IFS=$'\t' read -r A6_USER_ID A6_SYSTEM_ACCESS_TOKEN < <(
  jq -r --argjson merchant_id "${A6_MERCHANT_ID}" '
    .accounts[]
    | select(.merchant_id == $merchant_id)
    | [.a6_user_id, .system_access_token]
    | @tsv
  ' "${A6_CREDENTIAL_FILE}"
)

export A6_USER_ID A6_SYSTEM_ACCESS_TOKEN
test -n "${A6_USER_ID}" \
  && test -n "${A6_SYSTEM_ACCESS_TOKEN}"
```

### 11.2 读取供应商渠道

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${A6_SYSTEM_ACCESS_TOKEN}" \
  -H 'Accept: application/json' \
  -H "New-API-User: ${A6_USER_ID}" \
  'https://a6api.com/api/supplier/me/channels'
```

### 11.3 完整读取 Marketplace 页面

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${A6_SYSTEM_ACCESS_TOKEN}" \
  -H 'Accept: application/json' \
  -H "New-API-User: ${A6_USER_ID}" \
  'https://a6api.com/api/marketplace/channels/search?token_id=TOKEN_ID&model=MODEL&route_status=all&exclude_unavailable=false&include_seed=true&include_disabled=true&include_user_disabled=true&sort=input_asc&view=list&page=1&page_size=100'
```

必须继续翻页，直到达到返回的 `total`，不能只读取第一页。

### 11.4 Responses 非流式请求

```bash
curl --fail-with-body \
  -H 'Authorization: Bearer API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: REQUEST_ID' \
  'https://a6.a6api.com/v1/responses' \
  --data '{"model":"MODEL","input":"INPUT_TEXT","max_output_tokens":128,"reasoning":{"effort":"low"},"stream":false}'
```

## 12. 当前客户端方法索引

| Python 方法 | HTTP 接口 |
| --- | --- |
| `send_verification` | `GET /api/verification` |
| `register` | `POST /api/user/register` |
| `login` | `POST /api/user/login` |
| `self_user` | `GET /api/user/self` |
| `list_tokens` | `GET /api/token/` |
| `get_token` | `GET /api/token/{token_id}` |
| `update_token`, `rename_token` | `PUT /api/token/` |
| `ensure_one_key` | `GET/POST /api/token/`, `POST /api/token/{token_id}/key` |
| `fetch_models_via_a6` | `POST /api/supplier/fetch-models` |
| `apply_supplier` | `POST /api/supplier/apply` |
| `supplier_requests` | `GET /api/supplier/me/requests` |
| `supplier_me` | `GET /api/supplier/me` |
| `supplier_channels` | `GET /api/supplier/me/channels` |
| `update_supplier_channel` | `PATCH /api/supplier/me/channels/{channel_binding_id}` |
| `takedown_supplier_channel` | `POST /api/supplier/channels/{channel_binding_id}/takedown` |
| `supplier_listings` | `GET /api/supplier/listings` |
| `create_supplier_listings` | `POST /api/supplier/listings` |
| `update_supplier_listing` | `PUT /api/supplier/listings/{listing_id}` |
| `takedown_supplier_listing` | `POST /api/supplier/listings/{listing_id}/takedown` |
| `resubmit_supplier_listing` | `POST /api/supplier/listings/{listing_id}/resubmit` |
| `supplier_earnings_summary` | `GET /api/supplier/earnings/summary` |
| `supplier_earnings` | `GET /api/supplier/earnings` |
| `supplier_settlements_summary` | `GET /api/supplier/settlements/summary` |
| `supplier_settlements` | `GET /api/supplier/settlements` |
| `supplier_withdraw_summary` | `GET /api/supplier/withdraw/summary` |
| `supplier_withdraw_preview` | `GET /api/supplier/withdraw/preview` |
| `supplier_withdraw_submit` | `POST /api/supplier/withdraw` |
| `supplier_withdrawals`, `supplier_withdraw_status` | `GET /api/supplier/withdraw` |
| `supplier_withdraw_accounts` | `GET /api/supplier/withdraw/accounts` |
| `create_supplier_withdraw_account` | `POST /api/supplier/withdraw/accounts` |
| `balance_transfer_preview` | `GET /api/supplier/balance/transfer/preview` |
| `balance_transfer` | `POST /api/supplier/balance/transfer` |
| `user_logs` | `GET /api/log/self` |
| `marketplace_pins` | `GET /api/marketplace/pins` |
| `marketplace_channels` | `GET /api/marketplace/channels/search` |
| `marketplace_subscriptions` | `GET /api/subscriptions` |
| `marketplace_subscription` | `GET /api/subscriptions/{subscription_id}` |
| `marketplace_disable_channel` | `POST /api/marketplace/channels/{channel_id}/disable` |
| `marketplace_create_auto_pool` | `POST /api/subscriptions` |
| `marketplace_update_auto_pool` | `PUT /api/subscriptions/{subscription_id}/auto-pool` |
| `marketplace_pin` | `POST /api/marketplace/pin` |
| `marketplace_unpin` | `POST /api/marketplace/unpin` |
| `provider_responses_probe`, `provider_responses_request` | `POST /v1/responses` |
| `provider_chat_completions_probe` | `POST /v1/chat/completions` |

Typed Client 完整覆盖时还需新增以下已确认方法，不能把它们误写成当前已有封装：

| 待新增 Python 方法 | HTTP 接口 |
| --- | --- |
| `status` | `GET /api/status` |
| `generate_system_access_token` | `GET /api/user/token` |
| `delete_token` | `DELETE /api/token/{token_id}` |
| `update_supplier_profile` | `PATCH /api/supplier/me/profile` |
| `platform_models` | `GET /api/platform-models` |
| `marketplace_circuit_states` | `GET /api/marketplace/circuit-states` |
| `marketplace_hot_models` | `GET /api/marketplace/hot-models` |
| `marketplace_model_price_range` | `GET /api/marketplace/platform-models/{model}/price-range` |

## 附录 A：本项目 Trader Dashboard API

这些路由由 `src/a6api_auto/trade_dashboard.py` 提供，不是 `a6api.com` 的外部接口。默认监听 `127.0.0.1:8765`。

### A.1 读取接口

| 方法 | 路径 | 参数 / 鉴权 | 用途 |
| --- | --- | --- | --- |
| GET | `/healthz` | 无 | Dashboard 与仓储健康状态 |
| GET | `/metrics` | `Authorization: Bearer INTERNAL_TOKEN` | Prometheus publication 指标 |
| GET | `/api/overview` | `window` | 经营总览 |
| GET | `/api/merchants` | `window` | 商户列表与经营数据 |
| GET | `/api/market-traffic` | `window`, `model` | 市场流量、价格档和故障 |
| GET | `/api/merchants/{merchant_id}/credentials` | 外围私有访问控制 | 商户凭据投影 |
| GET | `/api/merchants/{merchant_id}/inventory` | 外围私有访问控制 | Supplier、Channel、Listing 清单 |
| GET | `/internal/provider-pools/{model}` | 外围私有访问控制 | Provider 池只读模型 |
| GET | `/internal/provider-inventory/{model}` | 外围私有访问控制 | Provider inventory |
| GET | `/internal/economic-priority1/{model}` | 外围私有访问控制 | Priority 1 经济计划 |

`/api/overview` 和 `/api/merchants` 的 `window` 可选值：

```text
5m, 1h, 5h, 12h, 24h, 7d, 30d, mtd, all
```

### A.2 Dashboard 操作接口

这些操作要求：

```http
Content-Type: application/json
X-Requested-With: a6-trader-dashboard
```

| 方法 | 路径 | JSON Body | 用途 |
| --- | --- | --- | --- |
| POST | `/api/accounts` | `username`, `password` | 导入 A6 账号 |
| PATCH | `/api/merchants/{merchant_id}/channels/{channel_id}` | `nickname?`, `description?`, `models?` | 更新渠道展示字段 |
| PATCH | `/api/merchants/{merchant_id}/listings/{listing_id}` | `display_name?`, `description?` | 更新 Listing 展示字段 |
| PUT | `/api/economic-priority1/{model}/profit` | `profit_percent` | 更新模型利润参数 |
| POST | `/api/merchants/{merchant_id}/retirement` | `{}` | 请求 Seller 退休 |

### A.3 内部 publication 控制接口

以下接口要求 `Authorization: Bearer INTERNAL_TOKEN`：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/internal/provider-attempt-failures` | 上报 Provider attempt failure |
| POST | `/internal/models/{model}/publication/reconcile` | 请求模型 publication reconcile |
| POST | `/internal/models/{model}/provider-quarantines/{quarantine_id}/probe` | 开始隔离 Provider 的恢复探测 |
| POST | `/internal/models/{model}/provider-quarantines/{quarantine_id}/recover` | 确认隔离 Provider 恢复 |

内部 JSON Body 上限为 16 KiB。错误响应统一为：

```json
{
  "error": {"code": "STABLE_ERROR_CODE"}
}
```

## 附录 B：本地 Provider relay

默认监听 `127.0.0.1:8767`，由 `src/a6api_auto/trade_upstream_relay.py` 提供。

| 方法 | 本地路径 | 上游映射 |
| --- | --- | --- |
| GET | `/healthz` | 本地 liveness，不访问上游 |
| GET | `/readyz` | 探测 `GET https://a6.a6api.com/v1/models` |
| GET/POST/PUT/PATCH/DELETE | `/a6-user/{a6_user_id}/v1[/...]` | 去掉 `/a6-user/{id}` 后转发到 `https://a6.a6api.com` |

示例：

```text
POST http://127.0.0.1:8767/a6-user/9517/v1/responses
  -> POST https://a6.a6api.com/v1/responses
```

relay 中的 `a6_user_id` 只用于请求归因，不改变上游 URL，也不从 Trader 代理池选择出口。上游固定为 `https://a6.a6api.com`。

## 附录 C：明确排除的依赖接口

以下服务被项目调用，但不属于 A6 API，因此未混入主清单：

- Enodot/Sub2API 管理接口：`https://ai.enodot.shop/api/v1/...`
- 上游模型服务：`UPSTREAM_BASE_URL/models` 或 `UPSTREAM_BASE_URL/v1/models`
- QQ 邮箱查询服务：`https://mail-api.zeabur.app/check`

它们分别由 `src/a6api_auto/enodot.py`、`src/a6api_auto/enodot_admin.py`、`src/a6api_auto/upstream.py` 和 `src/a6api_auto/mail.py` 管理。
