# S06 Provider 路由与流动性：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 0. 初始/人工/Staged Token route owner path

```text
apply_route_policy(op, account, token, desired_policy):       # RTE-001,006,007
    require caller is authorized admin command or S05 TOKEN_ROTATION child command
    require token Ban-domain membership present
    require TOKEN_BAN_INIT READY or onboarding gate operation owns this staged Token
    require desired_policy.mode in [AUTO_POOL, PIN]
    persist S06 desired route generation/operation before remote mutation; never auto-switch mode
    if AUTO_POOL:
        gate = S05.run_owned_exclusion_gate(op.pre_put_gate_child, [token], [desired_policy.model],
            require_effective_subscription=false)
        if gate not READY: keep paused/unpublished; return gate
        resolution = S01.execute_mutation(op, subscription pre-read, PUT auto-pool, exact subscription post-read)
    else:
        resolution = S01.execute_mutation(op, pin pre-read, POST pin, exact pins post-read)
    handle target/old/third/reject using the same resolution contract
    on target confirmation:
        if AUTO_POOL:
            require auto_channels non-empty and disjoint from S05 owned Channel IDs
            require current ban_membership_generation == gate.generation
        else:
            gate = S05.run_owned_exclusion_gate(op.pre_activation_gate_child, [token], [desired_policy.model],
                require_effective_subscription=true)
            require gate READY and exact Pin assignment Channel not owned
        facts = S04.commit_class1_route_facts(account, exact route readback)
        activate route only when facts cost/capability and route/model/callability all match
        if token is current: activation + account restore + Seller repricing/publication Outbox are one DB transaction
        if token is staged: persist confirmed route facts only; do not publish schedulability
    return confirmed activation facts or conservative pending/failure

configure_route_for_staged_token(parent_op, account, staged_token, old_route): # RTE-001
    desired = immutable copy of old route policy under staged token identity
    return apply_route_policy(child operation of parent_op, account, staged_token, desired)
```

## 1. 运行失败入口

```text
handle_provider_runtime_failure(account, error, fingerprint): # RTE-001..003,007,008
    begin transaction
    pause account with generation fence
    if canonical_error(error) == insufficient_quota:
        set UNFUNDED
        insert deduped liquidity-operation Outbox in SAME tx
        commit; return
    if account.route_mode == AUTO_POOL:
        insert unique expand-operation Outbox(account, route_generation, fingerprint) in SAME tx
    else if account.route_mode == PIN:
        insert unique repin-operation Outbox(account, assignment_epoch, fingerprint) in SAME tx
    never change route_mode
    commit
```

## 2. Auto-Pool 扩缩

```text
next_complete_band(snapshot, current_policy):                 # RTE-002,003,005
    require snapshot fresh, complete, callable and non-owned filtered
    current_boundary = current confirmed max_ratio
    bands = group sorted CNY ratios by exact Decimal value
    next = first band strictly greater than current_boundary
    if no next or next.ratio > Decimal("0.3"): return EXHAUSTED
    included_count = count(all items with ratio <= next.ratio)
    if included_count > max_n: return EXHAUSTED
    return {max_ratio:next.ratio, included_count, snapshot_generation}

reconcile_auto_pool_expand(op):                              # RTE-002,003,006,014
    account = authoritative account(op.account_id)
    route = current confirmed S06 route generation(account)
    require account paused and op still current for route.generation
    snapshot = fresh Observer full marketplace round
    desired = next_complete_band(snapshot, route.policy)
    if EXHAUSTED:
        transactionally set AUTO_POOL_RANGE_EXHAUSTED + insert 60s fresh-reevaluation Outbox; keep paused; return
    desired_route_generation = idempotently get/create immutable S06 route generation
        keyed by (account, ROUTE, op.identity) with exact desired payload hash;
        replay adopts same generation, later current -> SUPERSEDED
    gate = S05.run_owned_exclusion_gate(op.pre_put_gate_child,
        [current Provider Token], [route.model], require_effective_subscription=false)
    if gate not READY: keep paused; return gate
    result = S01.execute_mutation(op, subscription pre-read, PUT auto-pool, exact subscription readback)
    if result in [READBACK_INCOMPLETE, RETRY_LATER]: set ROUTE_UNKNOWN; keep paused; resolve by GET
    if result == REPLAY_SAME_OPERATION: set ROUTE_UNKNOWN; keep paused; replay same op only after exact old readback
    if result == REMOTE_DIVERGED: set ROUTE_UNKNOWN; supersede/replan from remote fact
    if result == REMOTE_REJECTED: keep paused; classify error; do not blind replay
    if result == CONFIRMED_ADOPT:
        facts = S04.commit_class1_route_facts(account, exact route readback)
        require auto_channels non-empty/disjoint, model/facts.cost/facts.capability/readiness exact
        require current ban_membership_generation == gate.generation
        begin transaction
        activate S06 route generation and restore account if no other pause reason
        insert immediate Seller repricing/publication Outbox in SAME tx
        commit

maybe_shrink_auto_pool(account, observation):                 # RTE-004
    require stable_for>=30m and since_change>=30m and successes>=100 and errors==0
    require cv_n>0.05 or tail_jump>0.03
    desired_target = target_n - 1, bounded by min_n
    recompute complete boundary band; never truncate equal band
    use the same one pre-PUT S05 gate + S01 mutation/readback + generation fence + S04 activation as expand;
    unknown -> ROUTE_UNKNOWN
```

## 3. Route unknown

```text
resolve_route_unknown(op):                                   # RTE-006
    remote = exact GET subscription with complete related facts
    if remote == op.desired: adopt generation
    else if remote == op.pre_state: replay same operation ID
    else supersede op and plan new generation from remote fact
    account remains paused until full activation readbacks pass
```

## 4. Pin

```text
reconcile_repin(op):                                         # RTE-007
    account = authoritative account(op.account_id)
    route = current confirmed S06 route generation(account)
    require route.mode == PIN and account paused and op.assignment_epoch == route.assignment_epoch
    snapshot = fresh callable marketplace; choose new non-owned Channel by route.Pin policy
    create S06 assignment_epoch+1; require current Provider Token instance TOKEN_BAN_INIT READY
    resolution = S01.execute_mutation(op, pin pre-read, POST pin, exact pins post-read)
    if CONFIRMED_ADOPT:
        gate = S05.run_owned_exclusion_gate(op.pre_activation_gate_child,
            [current Provider Token], [route.model], require_effective_subscription=true)
        if gate not READY: keep paused; return gate
        facts = S04.commit_class1_route_facts(account, confirmed Pin assignment/readback)
        require assignment non-owned and channel/facts.cost/facts.capability/model all match epoch
        begin transaction
        activate assignment generation and restore account if no other pause reason
        insert Seller repricing Outbox in SAME tx
        commit
    if REPLAY_SAME_OPERATION: keep paused; replay same op after exact old readback
    if REMOTE_DIVERGED: keep paused; supersede/replan from remote fact
    if READBACK_INCOMPLETE/RETRY_LATER: keep paused; retry readback
    if REMOTE_REJECTED: keep paused; classify; do not blind replay
```

## 5. Liquidity

```text
reconcile_liquidity(op):                                     # RTE-008..010
    account = authoritative account(op.account_id)
    funding_policy = immutable policy referenced by op.funding_policy_generation
    require account UNFUNDED and paused and op still current
    balances = exact read merchant withdrawable + user balance
    if op.transfer_intent absent:
        amount = floor_to_eligible_whole_units(balances.withdrawable,
                                                funding_policy.transfer_unit)
        if amount > 0:
            witness = run_liquidity_callback(op)
            if witness != VALID: record SKIPPED/INVALID_CALLBACK; keep UNFUNDED; return
            persist immutable transfer_intent(op.identity, amount, balances pre-hash, witness)
    if transfer_intent exists and not confirmed:
        require same persisted amount; never recompute it on retry
        preview exact persisted amount
        resolution = S01.execute_mutation(op, exact balance pre-reader,
                                          POST balance transfer(persisted amount), exact balance post-reader)
        if CONFIRMED_ADOPT: mark transfer intent confirmed with post balances
        if REPLAY_SAME_OPERATION: replay same intent only after exact-pre readback
        if REMOTE_DIVERGED or READBACK_INCOMPLETE or RETRY_LATER:
            keep UNFUNDED; reconcile same intent from balances; do not POST a new amount; return
        if REMOTE_REJECTED: keep UNFUNDED; record failure; return
    readback = exact current balances                           # defined even when amount==0
    if funding_contract(account, readback) passes:
        gate = S05.run_owned_exclusion_gate(op.reactivation_gate_child,
            [current Provider Token], [route.model], require_effective_subscription=true)
        require gate READY and route/callability/cost/capability readbacks current
        begin transaction
        clear UNFUNDED by owner; restore account only if no other pause reason
        insert schedulability/eligibility reprojection Outbox in SAME tx
        commit
    else:
        transactionally remove Provider duty and project slot VACANT
        enqueue funded replacement request in same transaction

admit_replacement(slot, account):                        # RTE-010
    require funded, native schedulable, capability current, cost current,
            Observer route current and S05 current EXCLUSION_GATE READY
    CAS slot VACANT -> account
```

## 6. Callback Token

```text
run_liquidity_callback(op):                                  # RTE-011,012
    token = require distinct callback duty and valid corpus name
    candidates = A6-owned small-merchant Channels with exact listed+callable readbacks
    if none: record SKIPPED_NO_ELIGIBLE_CHANNEL; return without request
    pin_resolution = S01.execute_mutation(op child, callback-pin pre-read, POST pin, exact pin readback)
    if pin_resolution != CONFIRMED_ADOPT: return NO_VALID_WITNESS
    response = POST /v1/responses input EXACT_PRODUCTION_PING
    require realistic sampled usage and configured high-cache invariant
    reject prompt_tokens=1, completion_tokens=0, total_tokens=1
           or other probe-shaped usage
    only then persist and return VALID callback witness for the same liquidity operation
```

## 7. Shadow guard

```text
maybe_run_provider_shadow(reason, required_route_change):     # RTE-013
    require timer is disabled
    reusable = current active or already-planned generation satisfies operation
    if reusable: return reusable without running shadow
    require required_route_change and explicit persisted concrete reason
    run service one-shot; do not enable timer or wait optional observation window
    finally require service inactive and timer disabled readback
```

## 需求覆盖索引

本稿实现：`RTE-001`, `RTE-002`, `RTE-003`, `RTE-004`, `RTE-005`, `RTE-006`, `RTE-007`, `RTE-008`, `RTE-009`, `RTE-010`, `RTE-011`, `RTE-012`, `RTE-013`, `RTE-014`。
