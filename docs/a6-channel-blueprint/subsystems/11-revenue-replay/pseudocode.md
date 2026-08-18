# S11 收益对账与 Replay：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. 收益读取计划

```text
next_earnings_poll(intent, now):                              # REV-007
    age = now - intent.created_at
    if age < 10m: return last_poll + 15s
    if age < 24h: return last_poll + 5m
    return DAILY_SWEEP

load_remote_settlement_fact(credential, request_id):          # REV-001,005,011
    earnings = S01 complete supplier earnings for relevant bounded window
    settlements = S01 complete settlement records required by finality contract
    persist completeness/hash for BOTH datasets
    if either dataset incomplete: return ABSENT_INCOMPLETE with evidence
    matches = exact request_id facts across both datasets
    if conflicting identities/hashes: return CONFLICT
    if any authoritative success fact found: return FOUND(canonical joined fact)
    return ABSENT_COMPLETE
```

## 2. 对账

```text
safe_ratio_check(numerator, denominator, target):             # REV-003,004
    if denominator == 0:
        if numerator == 0: return NOT_APPLICABLE_ZERO_BASE
        return HARD_DEVIATION_ZERO_BASE
    return Decimal(numerator) / Decimal(denominator) compared with target

reconcile_intent(intent_id):                                  # REV-001..006,008,009
    intent = load billing intent + winner identity by intent_id
    lock unique (intent.credential, intent.request_id)
    intent = reload winner snapshot, decisions, usage, billing and frozen vector under lock
    if intent.state == PREPARED:
        if now < intent.terminal_owner_deadline or terminal owner still has live write authority:
            reschedule at/after owner deadline; return
        S10.mark_orphan_prepared_delivery_unknown(intent.id, expired-owner witness)
        intent = reload intent                                  # now DELIVERY_UNKNOWN or concurrent terminal result
    remote = load_remote_settlement_fact(intent.credential, intent.request_id)
    if remote == CONFLICT:
        append conflict observation; reject settlement; alert; return
    if remote == ABSENT_INCOMPLETE: schedule next poll; return
    if remote == ABSENT_COMPLETE:
        if beyond configured finality window:
            if intent.state in [TERMINAL_WRITTEN, DELIVERY_UNKNOWN]:
                begin transaction
                S10.transition_intent(intent.id,
                    from_set=[TERMINAL_WRITTEN, DELIVERY_UNKNOWN],
                    to=VOID, evidence=complete remote-absence snapshot)
                insert immutable tombstone + late-sweep index in SAME tx
                commit
            else if intent.state == BILLED:
                append immutable missing-remote-finality anomaly; keep BILLED and include in late sweep
        else schedule next poll
        return
    require remote == FOUND and remote request identity matches intent
    if intent.usage_status == MISSING_REQUIRED_SCHEMA:
        append immutable exact account/profile schema-failure observation
        ensure S09 exact transport revocation is current/idempotent
        if remote.canonical_usage is missing or invalid for S07 native base-cost resolver:
            mark visible reconciliation dead-letter(reason=MISSING_CANONICAL_USAGE, intent_id)
            keep intent DELIVERY_UNKNOWN; do not guess usage/delta/debit; return
        resolved_usage = canonicalize(remote.canonical_usage)
        resolved_base = S07.resolve_base_actual_cost(
            resolved_usage, intent.snapshot.normalized_model,
            intent.snapshot.model_price_generation, group=ToA6API)
        begin transaction
        insert immutable late-usage-resolution(intent_id, hash(resolved_usage), resolved_base, remote evidence)
            on conflict verify identical
        commit
        S10.bill_winner(intent, evidence=S11_EARNINGS_CONFIRMED,
                        resolved_base_actual_cost=resolved_base)
        CAS BILLED -> RECONCILED
        return
    require remote usage/request identity hash matches intent
    tolerance = max(1 micros, intent.solver.overshoot,
                    S07.price_vector_quantization_envelope(intent.snapshot))
    local_check = safe_ratio_check(intent.ActualCost, intent.base_actual_cost, target=(1+intent.delta_applied))
    earning_expected = earning_from(intent.final_usage, intent.frozen_integer_price_vector)
    earning_check = deviation_after_absolute_tolerance(remote.earned_micros, earning_expected, tolerance)
    public_check = safe_ratio_check(remote.public_quota, remote.supplier_earning, target=Decimal("1.2"))
    checks = {local_check, earning_check, public_check}
    append immutable reconciliation observation with all three checks
    scope_and_record_deviation(intent, checks)
    if intent.state == RECONCILED: return idempotent no-op
    if billing exists and intent.state == BILLED: CAS BILLED -> RECONCILED
    else if no billing and remote proves success:
        S10.bill_winner(intent, evidence=S11_EARNINGS_CONFIRMED)
        CAS BILLED -> RECONCILED
    else if billing exists but state/hash inconsistent:
        append HARD reconciliation conflict; do not mutate ledger
```

## 3. 迟到记录

```text
daily_late_arrival_sweep():                                  # REV-006,007,012
    candidates = bounded union of:
        VOID tombstones not permanently resolved
        + BILLED but not RECONCILED intents whose remote finality record was missing/late
    for intent in candidates:
        remote = load_remote_settlement_fact(intent.credential, intent.request_id)
        if remote != FOUND: append sweep observation as applicable; continue
        verify original intent/usage hash before mutation
        if intent.state == VOID and no late adjustment exists:
            begin transaction
            S10.transition_intent(intent.id, from_set=[VOID], to=BILLED,
                evidence=S11_LATE_ARRIVAL_CONFIRMED)
            insert exactly one late UsageBilling/ledger adjustment with stable late-adjustment identity
            append late observation
            insert RECONCILE_INTENT Outbox in SAME tx
            commit                               # never call remote reconciliation inside this tx
        else if intent.state == BILLED:
            begin transaction
            append latency anomaly only; no new debit
            insert RECONCILE_INTENT Outbox in SAME tx
            commit
```

## 4. 偏差作用域

```text
scope_and_record_deviation(intent, checks):                   # REV-008,009
    begin transaction
    if checks.price_vector_or_markup_shared_failure:
        record immutable Listing-scoped deviation
        repair_owner = S07 CONFIG_INVALID
    else if checks.delta_or_schema_failure tied to account/profile:
        record immutable exact account/profile deviation
        repair_owner = S09 TRANSPORT_REVOCATION
    else if checks.only_class2_adjustment_failure:
        record immutable Class2Adjustment observation; repair_owner = NONE
    independent_count = unique request_ids in 15m with same scope/fingerprint under transaction snapshot
    if repair_owner != NONE and (one relative deviation >5% or independent_count >=3):
        insert deduped owning repair Outbox(scope, fingerprint, current source generation) in SAME tx
    commit
```

## 5. Replay

```text
replay_request(request_id, reason):                           # REV-002,010
    # Explicit business Replay only; never automatic process-restart continuation of a dead HTTP/stream connection.
    load original winner claim, intent, snapshots and compositor version
    if terminal can be safely redelivered without new upstream:
        reuse final bytes/hash and same delivery identity; never rebill
    else if new upstream attempt required and no downstream content commitment and winner_claim absent:
        candidate = S09.select_and_freeze_attempt(current hard gate, mode=REPLAY)
        link new attempt to original request lineage
        S10 winner CAS still uses original request_id
    else if winner_claim exists:
        redeliver/reconcile existing winner only; never create a doomed second upstream attempt
    else wait for earnings/reconciliation
```

## 需求覆盖索引

本稿实现：`REV-001`, `REV-002`, `REV-003`, `REV-004`, `REV-005`, `REV-006`, `REV-007`, `REV-008`, `REV-009`, `REV-010`, `REV-011`, `REV-012`。
