# HK Provider regional runtime

This directory stages the R1 Provider-only regional coordination topology. It does not replace
Sub2API's global Redis: `REDIS_HOST=127.0.0.1` and `REDIS_PORT=16379` continue to reach shared
non-Provider state through the US tunnel, while every Provider dispatch, fast-block, readiness and
capacity operation uses the independent HK Valkey at `127.0.0.1:6380` through `PROVIDER_REDIS_*`.

The checked-in deployment script only writes inactive units, private environments and a staged
Sub2API drop-in. It does not change either `current` symlink, run `systemctl enable/start/restart`,
modify Caddy/LB configuration, or expose port `48769` beyond loopback.

## Preconditions

1. The HK host has a real `valkey-server` package and its `valkey` account. The old
   `a6-shadow-redis.service` fixture must remain inactive. Valkey must be 7.x or newer because the
   runtime ACL uses selectors to separate read-only projection keys from writable lease keys.
2. `/opt/a6-trader-hk/current` contains the same reviewed Trader release as US, including
   `scripts/project_a6_provider_dispatch.py` and the `trade` optional dependencies. This is a
   separate HK release path; staging does not repoint it.
3. Migration `0042_provider_regional_coordination.sql` is applied. PostgreSQL logins
   `a6_provider_projector` and `a6_provider_subscriber` already exist, and the conditional grants in
   migration 0042 have been applied after those roles were created.
4. Sub2API's public schema includes migration 194 and the candidate binary contains the dedicated
   Provider Redis client, regional subscriber, capacity reconciler and regional readiness checks.
   The client must map `PROVIDER_REDIS_USERNAME` to the Redis ACL username; a password-only client
   is incompatible with the staged `default off` policy and must not be activated.
5. The canonical store has an active routing/cost generation for `gpt-5.6-sol`, and at least one
   current HK capacity allocation has positive quota. Across US and HK, quota must not exceed the
   account's declared concurrency.
6. HK remains loopback-only with LB weight `0` for staging, activation and restart tests.

Check the database roles before activation:

```sql
SELECT rolname FROM pg_roles
WHERE rolname IN ('a6_provider_projector', 'a6_provider_subscriber')
ORDER BY rolname;

SELECT
    has_table_privilege(
        'a6_provider_projector',
        'a6_trade.provider_region_projection_receipts',
        'SELECT,INSERT,UPDATE'
    ) AS projector_projection_receipts,
    has_table_privilege(
        'a6_provider_projector',
        'a6_trade.provider_region_capacity_receipts',
        'SELECT,INSERT,UPDATE'
    ) AS projector_capacity_receipts,
    has_table_privilege(
        'a6_provider_subscriber',
        'a6_trade.provider_region_projection_receipts',
        'SELECT,UPDATE'
    ) AS subscriber_projection_ack,
    has_table_privilege(
        'a6_provider_subscriber',
        'a6_trade.provider_region_capacity_receipts',
        'SELECT,UPDATE'
    ) AS subscriber_capacity_ack;
```

## Stage

Create a root-owned mode-0600 file from `hk-provider-regional.secrets.env.example`. Use distinct
Valkey runtime/projector passwords and the two dedicated PostgreSQL roles. Then run:

```bash
install -o root -g root -m 0600 hk-provider-regional.secrets.env \
  /etc/a6-provider-regional-hk.secrets.env
./deploy/hk-shadow/stage-provider-regional-runtime.sh \
  /etc/a6-provider-regional-hk.secrets.env
```

For a filesystem-only rehearsal, set `DESTDIR`; this is also useful for reviewing every rendered
file without changing systemd:

```bash
DESTDIR=/tmp/a6-provider-hk-stage \
  ./deploy/hk-shadow/stage-provider-regional-runtime.sh \
  /absolute/path/to/hk-provider-regional.secrets.env
find /tmp/a6-provider-hk-stage -type f -print
```

The regional Sub2API drop-in remains at
`/etc/a6-provider-regional-hk/staged-systemd/sub2api-hk-shadow-provider-regional.conf`. Installing
that file is the explicit activation boundary.

The generated ACL disables `default`. `a6-provider-runtime` can read dispatch/fast-block
projections and can mutate only capacity/readiness lease keys; `a6-provider-projector` can mutate
the Provider projection namespace. The verifier exercises both the permitted and rejected cases
against the running Valkey before checking receipts.

## Loopback activation

After the preconditions are true and the staged diff is approved, activate only the loopback
candidate in this order:

```bash
systemctl daemon-reload
systemctl enable --now a6-provider-valkey-hk.service

install -d -m 0755 /etc/systemd/system/sub2api-hk-shadow.service.d
install -m 0644 \
  /etc/a6-provider-regional-hk/staged-systemd/sub2api-hk-shadow-provider-regional.conf \
  /etc/systemd/system/sub2api-hk-shadow.service.d/70-provider-regional.conf
systemctl daemon-reload
systemctl restart sub2api-hk-shadow.service
systemctl enable --now a6-trader-provider-regional-hk.timer
```

The drop-in clears the legacy `60-local-shadow-redis.conf` dependency and environment-file list.
It retains the US tunnel for PostgreSQL and global application Redis, then adds the independent
Provider Valkey. Confirm that the legacy fixture did not remain active:

```bash
test "$(systemctl is-active a6-shadow-redis.service || true)" != active
```

## End-to-end receipt gate

The first projector cycle writes Redis and a `projecting`/`resyncing` receipt. The Go subscriber
then exact-reads the projected document and advances the durable acknowledgement. A later projector
cycle verifies the acknowledgement and marks it `ready`. The same handshake applies to capacity.

Run the regional-only gate while unrelated scheduler/full-readiness work is still settling:

```bash
REGIONAL_ONLY=1 ./deploy/hk-shadow/verify-provider-regional-runtime.sh gpt-5.6-sol
```

The command proves all of the following in one run:

- Provider and global Redis endpoints have distinct `INFO SERVER run_id` values.
- The Provider ACL rejects a non-Provider key.
- dispatch and fast-block Redis metadata match HK, the model and the live restart epoch.
- both projection receipts are `ready`, subscriber-connected and cursor-complete.
- at least one positive HK capacity allocation has an exact `ready` capacity receipt.
- `/healthz` is `200` and all four `regional_*` readiness checks are `ok`.

Require the complete candidate gate, including `/readyz=200`, by omitting `REGIONAL_ONLY`:

```bash
./deploy/hk-shadow/verify-provider-regional-runtime.sh gpt-5.6-sol
```

Keep LB weight at `0` after this passes. Redis restart/resync, strict HTTP/SSE/WebSocket A-to-B
samples, 24-hour path evidence and the documented `<250 ms` P95 gate remain separate prerequisites
for any traffic weight.

## Rollback

Rollback preserves Valkey data and PostgreSQL receipts for audit:

```bash
systemctl disable --now a6-trader-provider-regional-hk.timer
rm -f /etc/systemd/system/sub2api-hk-shadow.service.d/70-provider-regional.conf
systemctl daemon-reload
systemctl restart sub2api-hk-shadow.service
systemctl disable --now a6-provider-valkey-hk.service
```

This restores the base HK shadow environment and its default-off regional flags. It does not touch
the US service, either release symlink, or the load balancer.
