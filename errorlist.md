# Error 分类汇总

本文只汇总错误分类、错误码、常见文本和上下文分流，不包含处置动作、状态机、恢复流程或控制器设计。

## 1. `A6_ACCOUNT_RECOVERABLE`

A6 Provider 账号、受管凭据、额度或本地账号配置类错误。

### 1.1 额度与流动性

```text
insufficient_quota
account_quota_insufficient
merchant_quota_insufficient
provider_liquidity_insufficient
```

常见正文：

```text
账户额度不足
账户余额或订阅额度不足
额度不足，请充值
insufficient quota
account balance or subscription quota
```

### 1.2 受管 Provider 凭据与账号状态

仅当 Key/Token 属于受管 A6 Provider 时归入本类。

```text
invalid_api_key
provider_key_unauthorized
token_disabled
token_expired
unauthorized
internal_api_key_unauthorized
```

### 1.3 受管账号配置漂移

```text
insufficient_user_quota
local:insufficient_quota
local_insufficient_quota
pin_write_failed
pin_readback_mismatch
```

---

## 2. `UPSTREAM_AUTO_POOL_EXPAND`

已明确归因于当前外部 Channel、Provider、部署或 Auto-Pool 路由范围的错误。

### 2.1 Channel 与固定商家不可用

```text
fixed_merchant_unavailable
no_available_channel
subscription_channel_unavailable
channel_cooling
channel_unavailable
```

`fixed_merchant_unavailable` 已在以下 HTTP 状态中出现，分类以 code/message 为准：

```text
402
429
502
503
```

### 2.2 模型能力与部署

仅当模型是有效 Listing 模型，而当前 Channel 缺少能力或部署异常时归入本类。

```text
capability_mismatch
model_not_available
deploymentnotfound
model_price_error
```

### 2.3 Provider 执行与协议

```text
provider_upstream_unavailable
upstream_unavailable
do_request_failed
bad_response_body
structured_stream_error
missing_terminal_event
rate_limit_exceeded
auth_unavailable
relay_upstream_transport_failed
```

以下错误需要明确的 Channel/Provider 归因证据：

```text
upstream_unavailable
missing_terminal_event
relay_upstream_transport_failed
rate_limit_exceeded
```

可用归因证据类型：

```text
错误中包含明确 channel_id 或 provider code
同请求切换其他 Channel 后成功
当前 Channel 的直接 Probe 同样失败
故障集中于单个 Channel 或 Provider
```

---

## 3. `USER_SHORT_CIRCUIT`

用户输入、入口鉴权、内容策略或客户端中断类错误。

### 3.1 请求格式与参数

```text
bad_request
invalid_json
invalid_messages
invalid_parameter
invalid_prompt
invalid_request
invalid_request_error
invalid_value
malformed_request
request_too_large
unsupported_client
unsupported_parameter
thinking_signature_invalid
invalid_encrypted_content
```

### 3.2 上下文超限

```text
context_limit_exceeded
context_length_exceeded
context_too_large
input_too_long
```

### 3.3 内容与策略

```text
content_policy_violation
cyber_policy
moderation_blocked
session_blocked_by_cyber_policy
```

### 3.4 客户端中断

```text
user_cancelled
client_cancelled
context_canceled
downstream_client_cancelled
HTTP 499
```

### 3.5 用户入口鉴权

仅当错误来自用户入口 API Key 或 Session 时归入本类。

```text
unauthorized
invalid_api_key
```

### 3.6 用户侧模型与限流

```text
model_not_available
capability_mismatch
rate_limit_exceeded
```

---

## 4. `LISTING_COOLDOWN_TRIGGER`

A6 平台、网络路径、本地依赖、聚合终态，以及尚无明确账号或 Channel 责任证据的错误。

### 4.1 A6 平台、Endpoint 与网络路径

```text
cf_1010
cloudflare_1010
cloudflare_forbidden
egress_unavailable
endpoint_unavailable
platform_overloaded
bad_gateway
gateway_timeout
internal_server_error
service_unavailable
a6_transport_failed
a6_request_deadline_exceeded
```

常见信号：

```text
Cloudflare 403/1010
TLS timeout
curl code 28
connection reset
平台级 502/503/504/521/524
所有账号同时请求失败
所有 Channel 经同一 endpoint 同时失败
```

### 4.2 未知错误或责任证据不足

```text
attempt_failed
bad_response
bad_response_status_code
connection_reset
server_error
server_is_overloaded
system_cpu_overloaded
timeout
upstream_error
upstream_failover
upstream_timeout
unknown_api_error
unknown_error
```

单次裸 HTTP 状态默认归入本类：

```text
408
425
429
500
502
503
504
524
```

### 4.3 本地依赖

```text
cache_unavailable
database_unavailable
dns_unavailable
redis_unavailable
relay_unavailable
routing_unavailable
scheduler_unavailable
```

### 4.4 聚合终态

```text
no_available_accounts
no_available_upstream
```

聚合终态不是底层根因，底层账号或 Channel 仍需分别归入第 1、2 或 4 类。

---

## 上下文分流表

| 原始错误 | 上下文 | 分类 |
|---|---|---|
| `unauthorized` / `invalid_api_key` | 用户入口 Key 或 Session | `USER_SHORT_CIRCUIT` |
| `unauthorized` / `invalid_api_key` | 受管 A6 Provider Token | `A6_ACCOUNT_RECOVERABLE` |
| `model_not_available` / `capability_mismatch` | 用户请求的模型无效 | `USER_SHORT_CIRCUIT` |
| `model_not_available` / `capability_mismatch` | Listing 模型有效，当前 Channel 缺少能力 | `UPSTREAM_AUTO_POOL_EXPAND` |
| `rate_limit_exceeded` | 用户自身限流 | `USER_SHORT_CIRCUIT` |
| `rate_limit_exceeded` | 当前外部账号或 Channel 限流 | `UPSTREAM_AUTO_POOL_EXPAND` |
| `rate_limit_exceeded` | A6 平台全局限流 | `LISTING_COOLDOWN_TRIGGER` |
| `timeout` / `upstream_timeout` / 5xx | 单次发生或责任证据不足 | `LISTING_COOLDOWN_TRIGGER` |
| `timeout` / `upstream_timeout` / 5xx | 已明确集中于当前 Channel/Provider | `UPSTREAM_AUTO_POOL_EXPAND` |
| `missing_terminal_event` | 首次发生或责任证据不足 | `LISTING_COOLDOWN_TRIGGER` |
| `missing_terminal_event` | 已明确集中于当前 Channel/Provider | `UPSTREAM_AUTO_POOL_EXPAND` |
| `upstream_unavailable` / `relay_upstream_transport_failed` | 责任证据不足 | `LISTING_COOLDOWN_TRIGGER` |
| `upstream_unavailable` / `relay_upstream_transport_failed` | 已明确集中于当前 Channel/Provider | `UPSTREAM_AUTO_POOL_EXPAND` |
| `fixed_merchant_unavailable` | 任意已知 HTTP 状态 | `UPSTREAM_AUTO_POOL_EXPAND` |
| `HTTP 499` / client cancel | 客户端主动中断 | `USER_SHORT_CIRCUIT` |

## 分类判定顺序

```text
1. 客户端取消或确定性用户错误
   -> USER_SHORT_CIRCUIT

2. 明确额度、Provider Token 或受管账号状态错误
   -> A6_ACCOUNT_RECOVERABLE

3. 明确归因到当前 Channel/Provider 的渠道、能力或运行错误
   -> UPSTREAM_AUTO_POOL_EXPAND

4. A6 平台、Cloudflare、endpoint、egress 或本地依赖错误
   -> LISTING_COOLDOWN_TRIGGER

5. 其余未知或证据不足的错误
   -> LISTING_COOLDOWN_TRIGGER
```
