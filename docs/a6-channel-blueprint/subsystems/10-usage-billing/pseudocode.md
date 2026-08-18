# S10 Usage 合成与本地计费：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Decision

```text
raw_input_usage(U0):                                         # BIL-010
    return U0.input_tokens if present else U0.prompt_tokens if present else MISSING

evaluate_delta(snapshot, U0):                                # BIL-001,010
    input = raw_input_usage(U0)
    if input is MISSING:
        if snapshot.delta_effective>0: return ERROR_REQUIRED_SCHEMA
        return Decision(rate=0, applied_rate=0, skip=missing_usage_no_delta, original_hash=hash(U0))
    if input < 1000: return Decision(rate=0, applied_rate=0, skip=input_usage_below_1000,
                                     shortfall=1000-input, original_hash=hash(U0))
    return Decision(rate=snapshot.delta_effective, applied_rate=snapshot.delta_effective,
                    policy_generation=snapshot.desired_pricing_generation,
                    original_hash=hash(U0))

evaluate_class2(account, U0):                                # BIL-001,012..014
    if not account.IsOpenAIOAuth():
        return Decision(rate=0, applied_rate=0, skip=not_oauth, original_hash=hash(U0))
    decision = invoke existing Class2Adjustment evaluator on U0, preserving shadow/apply/
               berserk/probe behavior and identifiers
    require decision is based only on U0 and attach original_hash=hash(U0)
    return decision
```

## 2. 一次合成 solver

```text
compose_once(U0, delta_decision, class2_decision, price_snapshot): # BIL-002,008,009
    require both decisions.original_hash == hash(U0)
    delta_rate = Decimal(delta_decision.applied_rate)
    class2_rate = Decimal(class2_decision.applied_rate)
    vector = price_snapshot.integer_vector
    if delta_rate == 0 and class2_rate == 0: return exact original bytes/U0, overshoot=0
    base_earning = earning_from(U0, vector)
    target = ceil_micros(base_earning * (1+delta_rate) * (1+class2_rate))
    constraints = endpoint_schema_invariants(U0)
    deficit = max(0, target - base_earning)
    # Exact bounded solver: work is a function of the small endpoint schema dimension set, never token magnitude.
    candidates = bounded_schema_candidates(
        U0, vector, constraints, deficit, deterministic_field_order)
    # Generator uses marginal micros + integer ceil/division and enumerates only the finite
    # single-/coupled-dimension boundary combinations required by this endpoint's invariants.
    # It MUST NOT increment token counts one-by-one.
    require candidate_evaluation_count <= configured_constant_bound_for_schema(constraints)
    final = choose exact minimum legal candidate with earning>=target, deterministic tie-break
    require invariants_hold(U0, final)
    actual = earning_from(final, vector)
    return final, {target, actual, overshoot=actual-target, solver_version}
```

## 3. Winner 与 terminal

```text
prepare_success_terminal(attempt, raw_response):              # BIL-003..005,007,011
    U0 = extract_and_validate_terminal_usage(raw_response, attempt.profile)
    if attempt.snapshot.requires_delta and U0 invalid:
        S09.revoke_delta_transport(attempt.account, attempt.profile, evidence)
        # Synthetic SSE prelude does not count; only an upstream-originated event commit closes failover.
        if attempt.stream.upstream_stream_committed == false: return FAILOVER_ALLOWED
        terminate_stream
        return prepare_missing_usage_delivery_unknown(attempt, evidence)  # durable; NO_SECOND_ATTEMPT
    delta = evaluate_delta(attempt.snapshot, U0)
    class2 = evaluate_class2(attempt.account, U0)
    price_snapshot = {integer_vector=attempt.snapshot.frozen_integer_price_vector,
                      generation=attempt.snapshot.confirmed_listing_price_generation,
                      hash=attempt.snapshot.frozen_price_vector_hash}
    base_actual_cost = S07.resolve_base_actual_cost(
        U0, attempt.snapshot.normalized_model,
        attempt.snapshot.model_price_generation, group=ToA6API)
    final, solver = compose_once(U0, delta, class2, price_snapshot)
    begin transaction
    winner = insert winner_claim(request_id unique, attempt_id, claim_identity) on conflict load existing
    if existing winner belongs another attempt/claim_identity: rollback; return LOSER_NO_BILL
    if existing winner belongs this same attempt/claim_identity:
        intent = load existing billing_intent and verify hashes/snapshot identical
        commit; return ADOPT_EXISTING_INTENT(intent)
    terminal_owner_deadline = now + configured bounded terminal-finality handoff window
    insert billing_intent(economic_snapshot_id unique, state=PREPARED,
        raw_usage=U0, final_usage=final, base_actual_cost,
        hashes, delta_decision=delta, class2_decision=class2, solver,
        usage_status=VALID,
        terminal_owner_identity=attempt.terminal_owner_identity, terminal_owner_deadline)
    insert deduped RECONCILE_INTENT Outbox(intent identity, not_before=terminal_owner_deadline) in SAME tx
    commit
    return prepared intent

prepare_missing_usage_delivery_unknown(attempt, evidence):      # BIL-004,005,007,011
    begin transaction
    winner = insert winner_claim(request_id unique, attempt_id, claim_identity) on conflict load existing
    if existing winner belongs another attempt/claim_identity:
        append HARD terminal-ownership conflict; commit; return NO_SECOND_ATTEMPT_REVIEW
    if existing winner belongs this same attempt/claim_identity:
        intent = load existing billing_intent
        require intent.state == DELIVERY_UNKNOWN and intent.usage_status == MISSING_REQUIRED_SCHEMA
        commit; return ADOPT_EXISTING_DELIVERY_UNKNOWN(intent)
    delta = Decision(required_rate=attempt.snapshot.delta_effective, applied_rate=0,
                     skip=missing_required_schema_after_content_commitment)
    class2 = Decision(rate=0, applied_rate=0, skip=missing_usage_after_content_commitment)
    insert billing_intent(economic_snapshot_id unique, state=DELIVERY_UNKNOWN,
        raw_usage=null, final_usage=null, base_actual_cost=null, solver=null,
        delta_decision=delta, class2_decision=class2,
        usage_status=MISSING_REQUIRED_SCHEMA,
        terminal_owner_identity=attempt.terminal_owner_identity,
        delivery_unknown_evidence=evidence)
    insert deduped RECONCILE_INTENT Outbox(intent identity) in SAME tx
    commit
    return DELIVERY_UNKNOWN_NO_SECOND_ATTEMPT

write_terminal(intent, downstream):                          # BIL-005,007,011
    reload intent by stable claim/delivery identity
    if state in [TERMINAL_WRITTEN, BILLED, RECONCILED]:
        require stored terminal hash/delivery identity matches; return ALREADY_CONFIRMED without another write
    if state == DELIVERY_UNKNOWN: return DELIVERY_UNKNOWN      # S11 decides; never blind-redeliver
    require state == PREPARED and caller == intent.terminal_owner_identity
    try write final response/terminal under stable delivery identity
    if confirmed write boundary:
        begin transaction
        CAS PREPARED -> TERMINAL_WRITTEN with exact delivery evidence
        insert deduped BILL_WINNER Outbox(intent, evidence=TERMINAL_CONFIRMED) in SAME tx
        insert/retain RECONCILE_INTENT Outbox in SAME tx
        commit; return TERMINAL_WRITTEN
    if uncertain or stream content already written:
        begin transaction
        CAS PREPARED -> DELIVERY_UNKNOWN with uncertain-write evidence
        insert/retain RECONCILE_INTENT Outbox in SAME tx
        commit; return DELIVERY_UNKNOWN
    never transition BILLED/RECONCILED/VOID back to a delivery state

mark_orphan_prepared_delivery_unknown(intent_id, witness):    # BIL-005,007
    begin transaction
    lock intent; require intent.state == PREPARED
    require now >= intent.terminal_owner_deadline and witness proves terminal owner no longer has live write authority
    require no confirmed terminal-write evidence exists
    CAS PREPARED -> DELIVERY_UNKNOWN with owner-expired witness
    append immutable transition evidence
    commit; return DELIVERY_UNKNOWN
```

## 4. 本地计费

```text
bill_winner(intent, evidence, resolved_base_actual_cost=null): # BIL-005..007,010,011
    require intent is unique winner; reload current state
    if state in [BILLED, RECONCILED]:
        require existing UsageBilling/ledger key and amount identical; return existing billing
    if evidence == TERMINAL_CONFIRMED:
        require state == TERMINAL_WRITTEN and intent.usage_status == VALID
    if evidence == S11_EARNINGS_CONFIRMED:
        require state in [TERMINAL_WRITTEN, DELIVERY_UNKNOWN]
    otherwise reject billing
    if intent.usage_status == VALID:
        require intent.base_actual_cost != null
        billing_base = intent.base_actual_cost
    else if intent.usage_status == MISSING_REQUIRED_SCHEMA:
        require evidence == S11_EARNINGS_CONFIRMED
        require resolved_base_actual_cost != null
        require intent.delta_decision.applied_rate == 0             # never pretend missing delta was applied
        billing_base = resolved_base_actual_cost
    delta_applied = intent.delta_decision.applied_rate
    cost = ceil_scale(billing_base * (1+delta_applied), 8)
    begin transaction
    insert UsageBilling(idempotency_key=intent.claim_identity,
                        ActualCost=cost, BalanceCost=cost) on conflict verify identical
    insert/apply one negative balance ledger entry with same key on conflict verify identical
    CAS intent current allowed source state -> BILLED using the same evidence guard
    commit
```

Class2 rate 永不出现在 `cost` 公式。失败 attempt 或 winner claim loser 不调用 `bill_winner`。

## 5. 状态迁移 guard

```text
transition_intent(id, from_set, to, evidence):                # BIL-005
    allowed = {
      PREPARED:[TERMINAL_WRITTEN,DELIVERY_UNKNOWN],
      TERMINAL_WRITTEN:[BILLED,VOID],
      DELIVERY_UNKNOWN:[BILLED only by S11_EARNINGS_CONFIRMED,VOID],
      BILLED:[RECONCILED],
      VOID:[BILLED only by S11 late-arrival reopen]
    }
    require current in from_set when caller supplied from_set
    require to in allowed[current] and owner/evidence authorized
    CAS state and append immutable transition journal
```

## 需求覆盖索引

本稿实现：`BIL-001`, `BIL-002`, `BIL-003`, `BIL-004`, `BIL-005`, `BIL-006`, `BIL-007`, `BIL-008`, `BIL-009`, `BIL-010`, `BIL-011`, `BIL-012`, `BIL-013`, `BIL-014`。
