# S09 Eligibility 与 Attempt 快照：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Evaluator

```text
evaluate_relationship(listing, account, model, profile):       # ELG-001..006
    require stable listing.normalized_model == model
    sk = Decimal(account.current_cost.sk)
    profit = Decimal(account.effective_profit)
    ck = Decimal(listing.current_confirmed_ck)
    required = sk * (1 + profit)
    delta_auto = max(0, required / ck - 1)
    override = nullable Decimal override for stable tuple
    if override != null: require 0 <= override <= 0.5
    delta = override if override != null else delta_auto
    structural = model_capable(account, model)
              and native_sub2_schedulable(account)
              and 0 <= delta <= 0.5
              and ck * (1 + delta) >= required
    requires_delta = delta > 0
    attempt = structural and
              (not requires_delta or delta_transport_capable(account, profile))
    return immutable Decision(inputs={listing_binding_id=listing.binding_id, account_id=account.id, model, profile, ck, sk, profit},
                              required, delta_auto, delta_effective=delta,
                              structural, attempt, reason_codes)
```

## 2. Required profiles 与 projection

```text
recompute_listing_eligibility(listing, adapter_generation):    # ELG-007..009
    required = adapter_generation.required_transport_profiles
    if required empty:
        begin transaction
        insert OutputConfig-owned INVALID command/fact + desired/reprojection Outbox for this source generation
        commit; return
    begin transaction
    for explicitly bound account and each profile in required:
        decision = evaluate_relationship(listing, account, listing.normalized_model, profile)
        upsert sparse eligibility only for candidate or explainable failure as policy
    per_profile_counts = count attempt=true
    if any required count == 0:
        ensure NO_ELIGIBLE source_generation=current composite hash
    else:
        clear only matching Eligibility-owned NO_ELIGIBLE
    write DB projection generation and deduped Redis/admission refresh Outbox in SAME tx
    commit
    after commit attempt fast Redis replacement; if process dies, Outbox rebuilds the same generation/hash

normalize_actual_profile(request):                            # ELG-006,008
    profile = endpoint_kind + stream_mode + terminal_usage_schema_version
    if profile not declared by current adapter generation:
        transactionally insert deduped OutputConfig-owned invalid/reprojection Outbox for current adapter generation
        reject this request before account penalty
    return profile
```

## 3. 候选选择与冻结

```text
current_candidates(listing_binding_id, normalized_model, profile): # ELG-009,010,012
    require all three inputs are explicit and normalized
    projection = read Redis candidate projection for exact listing_binding_id/model/profile
    require projection composite generation/hash matches current authoritative DB projection
    if stale/missing: return GENERATION_STALE, never silently use old candidates
    return candidates with attempt=true for exact profile plus projection generation/hash

select_and_freeze_attempt(request, selection_mode):            # ELG-010..012
    context = S02.build_dispatch_context(request.auth, request)
    profile = normalize_actual_profile(request)
    candidates = read current Redis projection for context.listing_binding_id + context.normalized_model
                 filtered attempt=true for exact profile
    ordered = native_scheduler_order(candidates, selection_mode)
    gate_batch = batch load authoritative current gate pointers/facts for ordered candidate IDs in one bounded query
    ordered = discard candidates whose projected composite generation/hash is stale against gate_batch
    for candidate in ordered:
        acquire native capacity slot
        begin transaction
        # Only the candidate about to dispatch pays the full authoritative fence cost.
        current = load/lock this candidate's authoritative generations + current S07 confirmed Listing price fact
        decision = evaluate_relationship(context.listing, candidate.account, context.normalized_model, profile)
        if projection/facts changed or decision.attempt == false:
            rollback; release slot; continue
        insert economic_snapshot(attempt_id unique,
            output_channel_generation, listing_generation, account_generation,
            route_generation, cost_generation, capability_generation,
            billing_generation, desired_pricing_generation,
            confirmed_listing_price_generation, frozen_integer_price_vector, frozen_price_vector_hash,
            model_price_generation, fx_generation, transport_profile_generation,
            delta_effective=decision.delta_effective, required=decision.required, ck=decision.inputs.ck,
            sk=decision.inputs.sk, profit=decision.inputs.profit, composite_hash)
        commit
        recheck pre-send generation fence
        if invalid before dispatch: mark snapshot NOT_DISPATCHED; release; continue
        hand {attempt_id, immutable economic_snapshot} to the existing native scheduler dispatch primitive
        # native scheduler's existing attempt/send state is the sole dispatch boundary;
        # S09 must not invent a second non-atomic "mark DISPATCHED then send" lifecycle.
        return attempt handle from native scheduler
    return no dispatch result classified by S12
```

## 4. Override

```text
set_delta_override(stable_tuple, nullable_value, expected_generation, change_identity, actor): # ELG-003,FND-011
    if nullable_value != null:
        require Decimal("0") <= Decimal(nullable_value) <= Decimal("0.5")
    payload_hash = canonical_hash(nullable_value)
    begin transaction
    lock exact stable_tuple = (listing_binding_id, account_id, normalized_model)
    existing = override generation by unique change_identity
    if existing:
        require existing.payload_hash == payload_hash
        if current override generation == existing: ensure eligibility-recompute Outbox exists; commit; return ADOPT_EXISTING
        if current override generation newer: commit; return SUPERSEDED
    require expected_generation == current override generation(stable_tuple)
    write new S09-owned override generation(nullable Decimal value, change_identity, payload_hash) and audit
    insert affected eligibility-recompute Outbox rows in SAME transaction
    commit
    return pending operation identity; preview is computed separately from same evaluator
```

## 5. Runtime capability revocation

```text
revoke_delta_transport(account, profile, evidence):            # ELG-013,FND-011
    change_identity = stable_hash(account.id, profile, evidence.failure_identity)
    payload_hash = canonical_hash(revoked=true, evidence.normalized_reason)
    begin transaction
    existing = exact revocation generation by unique change_identity
    if existing:
        require identical payload_hash; ensure affected eligibility-recompute Outbox exists; commit; return existing
    write S09-owned exact (account, transport_profile) revocation generation(change_identity, payload_hash) + evidence
    insert affected Listing eligibility-recompute Outbox rows in SAME transaction
    commit
    do NOT mutate S04 account capability generation; do not invalidate other profiles/accounts or S07 price generation
```

## 需求覆盖索引

本稿实现：`ELG-001`, `ELG-002`, `ELG-003`, `ELG-004`, `ELG-005`, `ELG-006`, `ELG-007`, `ELG-008`, `ELG-009`, `ELG-010`, `ELG-011`, `ELG-012`, `ELG-013`, `ELG-014`。
