# a6api Protocol Notes

Captured against the served `0723-fix-something` frontend on 2026-07-23.

## Account lifecycle

| Stage | Request |
| --- | --- |
| Public settings | `GET /api/status` |
| Send QQ OTP | `GET /api/verification?email=ADDRESS&turnstile=` |
| Register | `POST /api/user/register?turnstile=` |
| Login/relogin | `POST /api/user/login?turnstile=` |
| Validate session | `GET /api/user/self` |
| Create key metadata | `POST /api/token/` |
| Reconcile key | `GET /api/token/?p=1&size=100` |
| Reveal full key | `POST /api/token/{id}/key` |

Authenticated calls use the `session` cookie and `New-API-User: USER_ID`. The saved
username/password are the recovery boundary when the cookie expires.

The QQ correlation identity is exact:

- Sender display name: `A6API`
- Sender address: `noreply@notify.a6api.com`
- Subject: `A6API 邮箱验证码`
- Code: six decimal digits following a verification-code label

The implementation snapshots existing mail IDs before requesting a code, so an old code is not
reused.

## Supplier lifecycle

The current frontend module `supplier-BMB6c_Kw.js` exposes:

| Stage | Request |
| --- | --- |
| Discover models through a6api | `POST /api/supplier/fetch-models` |
| Initial supplier or additional channel request | `POST /api/supplier/apply` |
| Current supplier | `GET /api/supplier/me` |
| Reconcile requests | `GET /api/supplier/me/requests` |
| Current channels | `GET /api/supplier/me/channels` |
| Update profile | `PATCH /api/supplier/me/profile` |
| Update channel | `PATCH /api/supplier/me/channels/{id}` |

The application body is:

```json
{
  "supplier": {
    "name": "TEMPLATE_SUPPLIER_NAME",
    "contact_info": "REGISTERED_QQ_EMAIL",
    "description": "TEMPLATE_SUPPLIER_DESCRIPTION"
  },
  "channel": {
    "name": "TEMPLATE_CHANNEL_NAME",
    "nickname": "TEMPLATE_CHANNEL_NAME",
    "channel_type": 1,
    "base_url": "UPSTREAM_BASE_URL",
    "api_key": "UPSTREAM_API_KEY",
    "models": "COMMA_SEPARATED_DISCOVERED_MODELS",
    "description": "TEMPLATE_CHANNEL_DESCRIPTION"
  }
}
```

Model discovery first calls the supplied upstream directly. The a6api adapter is used only when
both `/models` and `/v1/models` fail. The upstream key is included in the application request but
is not copied into the session file.

## Enodot group key selection

The slow batch defaults to a bundled upstream-domain catalog and resolves a usable upstream Key
through the Sub2API admin API:

| Stage | Request |
| --- | --- |
| Resolve exact active group | `GET /api/v1/admin/groups/all` |
| List group Keys | `GET /api/v1/admin/groups/{id}/api-keys?page=N&page_size=200` |

These calls authenticate with `x-api-key` from `AI_ENODOT_SHOP_ADMIN_KEY`. The resolver requires
exactly one active group named `TPA6API`, filters for active Keys, and chooses one randomly for each
new account. The selected domain and Key ID are written before a6api mutation; retry and resume
resolve that same Key ID again. Full Enodot admin and upstream Key values are never serialized in
batch state or events.

## sing-box subscription proxy pool

Subscription URLs are loaded from an owner-only file and requested with `User-Agent:
sing-box/1.13.12`. Each returned configuration is sanitized before launch:

- TUN inbounds are removed and one loopback-only mixed inbound is retained.
- Legacy sniff fields and conflicting experimental controllers are removed.
- The urltest group with the broadest node membership becomes `route.final`.
- Provider configs are checked by the local sing-box binary before launch.
- Sanitized credential-bearing caches remain in `0600` files below a `0700` directory.

Each subscription becomes one `socks5h://127.0.0.1:PORT` entry in the batch proxy deck. sing-box
children are terminated when the batch exits. Compatibility environment variables for current
provider configs are scoped to those check/run subprocesses.
