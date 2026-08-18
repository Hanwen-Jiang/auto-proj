# A6 Channel 更新与恢复 Probe（2026-08-12）

> 目的：确认 `PATCH /api/supplier/me/channels/{binding_id}` 的 `api_key` 可写，以及
> `POST /api/supplier/channels/{binding_id}/restore` 的 Channel 级恢复能力。所有凭证、完整 Key 已脱敏。
> 旧 HAR（7-24-15-54.md）当时未抓到这两类请求，只代表当时未观测，不代表接口不存在。

## 1. 前置

- 使用项目受控 A6 商户（`a6_user_id` + `system_access_token` 凭据对）。
- 目标为单个隔离的 A6 自有 Channel；probe 期间该 Channel 不承载生产流量。
- 所有写入都完成 `GET /api/supplier/me/channels` 与 marketplace readback 复核。

## 2. PATCH 更新 Supplier Channel（含 api_key）

```bash
curl --fail-with-body -sS \
  -X 'PATCH' \
  -H "Authorization: Bearer ${A6_SYSTEM_ACCESS_TOKEN}" \
  -H "New-API-User: ${A6_USER_ID}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/plain, */*' \
  'https://a6api.com/api/supplier/me/channels/CHANNEL_BINDING_ID' \
  --data-raw '{
    "nickname": "CHANNEL_NICKNAME",
    "description": "CHANNEL_DESCRIPTION",
    "models": "MODEL_A,MODEL_B",
    "base_url": "UPSTREAM_BASE_URL",
    "test_model": "TEST_MODEL",
    "api_key": "UPSTREAM_API_KEY"
  }'
```

响应信封：`{"message":"","success":true}`（HTTP 200）。

写后核对：

```text
GET /api/supplier/me/channels
-> 同一 binding_id 的 base_url / models / test_model / api_key fingerprint 与本次写入一致
```

结论：

- `nickname / description / models / base_url / test_model / api_key` 均写成功（current probe confirmed）。
- `api_key` 可写意味着入口 Key Rotation 可走“Channel 级 PATCH”路径，无需逐 Listing takedown。

## 3. Channel takedown

```bash
curl --fail-with-body -sS \
  -X 'POST' \
  -H "Authorization: Bearer ${A6_SYSTEM_ACCESS_TOKEN}" \
  -H "New-API-User: ${A6_USER_ID}" \
  -H 'Content-Type: application/json' \
  'https://a6api.com/api/supplier/channels/CHANNEL_BINDING_ID/takedown' \
  --data-raw '{}'
```

响应信封：`{"message":"","success":true}`。

写后核对：`GET /api/supplier/me/channels` 确认 Channel 进入 down/下架状态；其子 `(Channel, Model)`
Listing 不再市场可见。

## 4. Channel restore

```bash
curl --fail-with-body -sS \
  -X 'POST' \
  -H "Authorization: Bearer ${A6_SYSTEM_ACCESS_TOKEN}" \
  -H "New-API-User: ${A6_USER_ID}" \
  -H 'Content-Type: application/json' \
  'https://a6api.com/api/supplier/channels/CHANNEL_BINDING_ID/restore' \
  --data-raw '{}'
```

响应信封：`{"message":"","success":true}`。

写后核对（与 API Reference §6.4 一致）：

```text
GET /api/supplier/me/channels            # Channel 恢复为 active/up
GET /api/marketplace/channels/search ... # 对应 listing 重新精确可见
```

## 5. 结论与影响

| 事实 | 证据 |
| --- | --- |
| `PATCH .../channels/{binding_id}` 可写 `api_key` | 本 probe §2，写后 readback 一致 |
| `POST .../channels/{binding_id}/restore` 存在且可用 | 本 probe §4 |
| takedown 与 restore 均空对象 Body | 本 probe §3/§4 |
| Channel 恢复后仍需 marketplace readback 复核 | 本 probe §4 |

由此：

- S03 入口 Key Rotation 改为 Channel-scope maintenance：takedown -> PATCH api_key -> restore，
  不再逐 Listing takedown/resubmit（见 Prompt §6.1、S03 LIF-008~010）。
- A6 API Reference §6.4 与 Typed Client 覆盖索引（`restore_supplier_channel`）以此 probe 为证据。

---
