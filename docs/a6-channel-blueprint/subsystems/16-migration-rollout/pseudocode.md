# S16 迁移与 `us_cloud` 发布：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. 远端只读预检

```text
preflight_us_cloud():                                        # MIG-001..003
    connect ssh alias us_cloud
    discover actual Sub2API repository/release root without mutation
    record git commit/version/dirty state and build metadata
    record migration head and DB/Redis connectivity using redacted references
    inventory service/timer/blue-green/proxy topology and current traffic owner
    verify Class2Adjustment API/config/binary behavior baseline
    assert provider pool shadow service is not active and timer disabled
    emit signed/timestamped deployment manifest; do not guess old /Users paths
```

## 2. Migration preflight 与 anchor

```text
build_legacy_channel_map():                                  # MIG-004,005
    inventory = complete A6 Supplier/Channel/Listing readbacks
    local = legacy users, groups, keys, bindings, success ingress logs
    group by remote_channel_id
    for group:
        candidates = score only deterministic evidence:
            exact binding, current remote key fingerprint/last4,
            successful ingress log, unique ownership
        if unique highest exact anchor: persist mapping and evidence
        else create BINDING_REVIEW; exclude from cutover

migrate_output_group(group):                                 # MIG-006
    inventory both TPA6API and ToA6API entities/members/accounts/keys
    if same entity renamed: canonicalize in place
    if two entities: merge by stable Channel mapping, validate unique ingress and projections
    set all multipliers Decimal("1.0")
    retire old entity/alias only after exact membership validation
```

## 3. Backfill

```text
backfill_channel(mapping):                                   # MIG-005,007,008
    begin transaction
    create/preserve stable channel/listing bindings and historical facts
    preserve anchor users.id/key where one-to-one
    split/merge users according to mapping; stage rotations, never duplicate effective keys
    for each remote Listing instance proven current/adoptable:
        S03.adopt_listing_instance_in_tx(tx, channel_binding, stable_binding, remote_listing, complete_inventory)
        # This atomically advances current instance + Ban-domain membership + generation + BAN_INIT_REQUIRED Outbox.
    for each proven retired/deleted/replaced non-routable historical instance:
        persist closed historical instance/tombstone audit only; never make it current or Ban-domain member
    copy legacy delta only for current candidates as nullable override with source tag
    persist old public text audit; allocate corpus selections ONLY for fields
        requiring a new remote mutation; immutable historical Supplier name -> LEGACY_REMOTE
    backfill deterministic merchant_price_rank per merchant, confirmable before cutover
    preserve old balance audit; insert opening adjustment so new A6 ledger starts at zero
    commit; do not delete history

backfill_input_accounts():                                   # MIG-009
    for each Provider remote Token proven current/adoptable:
        create explicit Class1 binding and, before any schedulable projection,
        enter Token through S05 Ban-domain transaction primitive (membership + generation + BAN_INIT_REQUIRED Outbox atomic)
    OAuth/API Key remain pending until admin confirms Class2/3 cost contract
```

## 4. Cutover

```text
cutover_channel(channel):                                    # MIG-010..012,014
    require all dependency package tests/evidence and channel not in review
    snapshot = capture local user/key/group, current remote Channel key,
               every Listing state/vector, generations and complete readback hashes
    persist immutable rollback snapshot
    S13 unlist all Listings and exact readback
    enable only this Channel feature flag in staged request plane
    require current unique Key, exact model bindings
    ban_gate = S05.run_owned_exclusion_gate(cutover operation child,
        every potentially schedulable Provider Token, channel models,
        require_effective_subscription=true)
    require ban_gate READY and current membership generation == ban_gate.generation
    require S07 price config and S09 required profile eligibility
    # Listings are intentionally unlisted here, so marketplace availability is NOT a pre-recovery gate.
    S13 recover Listings in controlled batches
    require each opened Listing obtained S13 marketplace visible+available + complete circuit-clear witness
    watch winner/billing/earnings and error/operation metrics
```

## 5. Rollback 分界

```text
rollback_channel(channel, snapshot):                         # MIG-011..014
    close admission first
    if no new winner ledger exists since cutover:
        restore old local read/dispatch path and snapshot facts
        restore exact single remote ingress key and Listing states/prices
        do not restore non-anchor keys
    else:
        forward rollback:
            preserve new winner intents, billing, instances, generations and negative balance
            restore compatible old read/scheduler path behind current unique remote key
            never delete new facts or resurrect old non-anchor key
    exact remote/local readback every restored item
    keep only failed channel rolled back; other flags unchanged
```

## 6. Acceptance runner

```text
run_release_gates():                                         # MIG-015..019
    collect tests from S00..S16 mapped by requirement ID
    run migration empty/history/idempotency/rehearsal suites
    run existing Sub2API and Class2Adjustment regression suites
    run request-mode, pricing, Provider, recovery and fault-injection matrices
    on authorized isolated resources collect A6 mutation/readback evidence
    execute one delta_required>0 request and join local billing to A6 earning
    verify exact callback contract only if liquidity operation naturally requires it
    verify provider shadow timer disabled
    produce operations parameter manifest and rollback commands for actual topology
    block release on any missing requirement/evidence
```

## 7. 批次推进

```text
rollout_batches(channels):                                # MIG-010,MIG-011,MIG-016
    start with one canary Channel/model
    advance only after configured observation and all gates, without waiting on Provider shadow
    process bounded Channel batches using current production change policy
    stop on first new severity threshold, close affected admission, choose full/forward rollback
```

## 需求覆盖索引

本稿实现：`MIG-001`, `MIG-002`, `MIG-003`, `MIG-004`, `MIG-005`, `MIG-006`, `MIG-007`, `MIG-008`, `MIG-009`, `MIG-010`, `MIG-011`, `MIG-012`, `MIG-013`, `MIG-014`, `MIG-015`, `MIG-016`, `MIG-017`, `MIG-018`, `MIG-019`, `MIG-020`。
