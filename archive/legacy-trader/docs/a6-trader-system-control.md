# A6 Trader system control

`a6-trader-control.target` is the single lifecycle owner for the production A6
Trader workload on `us_cloud`. The operator command is installed as:

```bash
sudo a6-trader-control start
sudo a6-trader-control stop
a6-trader-control status
a6-trader-control status --json
```

## Start boundary

Starting the target submits one systemd transaction containing:

- Public and internal A6 request planes on ports 48767, 48768 and 48769.
- The Provider upstream relay, Trader dashboard and Listing refresh panel.
- Observer, Pin, Provider Apply, failure repair and dispatch triggers.
- Collector, merchant recovery and session refresh triggers.
- Seller pricing, onboarding, growth, retirement and upstream guard triggers.
- Model publication, generation cleanup and market-history cleanup triggers.
- Cashout plan/apply, 30-second intent reconciliation and wallet-account reconciliation triggers.
- Priority 1 owned-account discovery, weekly quota sampling and economic projection triggers.

Workers behind timers, paths and `OnSuccess` edges are not started directly.

## Stop boundary

Stopping submits the target and its complete membership in one systemd stop
transaction. The stop boundary includes every trigger and every worker that may
already be executing. It additionally closes default-off Provider Shadow,
Provider Liquidity, Listing refresh, and registration batch units.

The stop command disables independent member autostart before stopping the live
units. This keeps the stopped state across a reboot. The start command also
normalizes member autostart and enables only the target.

PostgreSQL, Redis, Caddy, Sing-box and the shared Sub2API schedulers are host
infrastructure and are deliberately outside this lifecycle boundary.

## Regional boundary

The HK units are a loopback-only shadow/synthetic fixture. They are inventory,
not production start dependencies:

- `a6-provider-relay-hk.service`
- `a6-provider-replay-synthetic-fixture.service`
- `sub2api-hk-state-tunnel.service`
- `sub2api-hk-shadow.service`
- `sub2api-hk-candidate-48770.service`

`win_gw_lou` has no A6 Trader or Sub2API runtime in the current deployment.
