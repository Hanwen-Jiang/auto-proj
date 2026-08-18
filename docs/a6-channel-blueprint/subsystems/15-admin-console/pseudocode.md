# S15 管理端与 Read Model：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Read model

```text
list_admin_users(filter, cursor):                            # ADM-001,002,012
    if filter.user_type == a6-channel:
        query users + stable channel + aggregated listing/eligibility/hold facts
    else preserve existing query semantics
    serialize negative Decimal balance, statuses, freshness,
              key fingerprint/last4 only

get_a6_channel_drawer(user_id):                              # ADM-003,008
    batch load Listings, price facts, hard bounds, eligibility relations,
               generations, holds, operations, incidents, readback metadata
    return read_model(as_of, DB generation, projection generation, stale flags)
    perform no remote calls in per-user request
```

## 2. 数值预览与提交

```text
preview_policy_change(command):                              # ADM-005,007
    parse all numeric values from decimal strings; reject float JSON numbers by contract
    snapshot = clone current immutable facts without writing owner tables
    owner = map command type: delta override->S09; profit/Class2/Class3 economic policy->S04;
            hard bounds->S08; route mode/params->S06
    proposed = owner.preview_command(snapshot, command) using the SAME production validator/evaluator
    run only affected downstream production evaluators (S08 price band/targets and/or S09 eligibility)
        against proposed immutable facts
    return before/after counts, per-profile reasons, price bounds, affected resources

submit_policy_change(command, actor):                        # ADM-004,005,012
    authorize department + action + resource scope
    owner = map command type: delta override->S09; profit/Class2/Class3 economic policy->S04;
            hard bounds->S08; route mode/params->S06
    require command.expected_generation == owner.current_generation(command.resource)
    enqueue typed owner command through S14 with command.expected_generation;
        S15 writes no owner current-generation table
    return operation identity and PENDING, not remote success
```

## 3. Corpus 管理

```text
list_corpus_choices(resource, kind):                         # ADM-006
    return S00 valid unused entries plus provenance, no free-text field;
    LEGACY_REMOTE entries rendered read-only
    (source=LEGACY_REMOTE, mutable=no, activation_blocking=no)

select_or_randomize_corpus(resource, choice?, actor):        # ADM-006
    authorize output/input department based on resource
    call S00 selection path that persists selection + owning-resource reconcile Outbox in one DB transaction
    return pending operation; after-commit notification is optional only
```

```text
list_merchant_price_ranks(scope):                            # ADM-013
    return per-merchant a6_merchant_price_orders rank/generation
        with current price band preview (read model only)

preview_merchant_price_rank_change(proposal, actor):         # ADM-013
    run production S08.build_targets under proposed rank assignment
    return before/after per-merchant target slices and delta distribution impact

submit_merchant_price_rank_change(proposal, expected_generation, actor): # ADM-013
    authorize admin policy scope
    require expected_generation == current S08 merchant_price_order_generation
    enqueue S14 command:
        kind = SET_MERCHANT_PRICE_ORDER
        owner = S08
        proposal
        expected_generation
        actor
    return operation pending                    # S15 never writes the generation

merchant_risk_profile(credential):                           # ADM-014
    # Pure read projection only: no timer/controller/command/side effect.
    derive only from S10/S11/S12 historical facts:
        delta_request_ratio, average_delta, high_delta_ratio,
        delta_adjusted_volume, earnings anomaly, platform warning,
        merchant suspension/takedown events -> risk_score
    read-only; never passed to S08/S09/S10
```

## 4. Operations

```text
replay_operation(op_id, expected_generation, actor):         # ADM-008,009
    authorize operation department
    op = load operation
    require expected_generation == op.desired_generation == current desired generation(op.resource)
    return S14.replay_dead_letter(op_id, actor); retain identity/history

resolve_binding(review_id, action, instance_id?, expected_generation, actor): # ADM-010
    require action in [SELECT_INSTANCE, ACCEPT_REMOTE_NEW_GENERATION, RETIRE]
    require exact readback evidence for selected/accepted instance when applicable
    require expected_generation == current Binding review/source generation
    map action to S02/S03 Binding owner command with review_id, expected_generation and evidence
    enqueue that typed command through S14; S15 writes neither current binding nor observed row
    return operation pending

set_operator_hold(listing, enabled, expected_generation, actor):          # ADM-010
    authorize OPERATOR_HOLD for actor
    require expected_generation == current Operator source generation(listing, actor scope)
    if enabled:
        S13.ensure_hold(listing, OPERATOR_HOLD, owner=Operator, source_key=actor policy scope, generation=expected_generation)
    else:
        S13.clear_hold(listing, OPERATOR_HOLD, owner=Operator, source_key=actor policy scope,
                       generation=expected_generation, witness=authorized operator clear command)
```

## 5. A6 console

```text
a6_console_request(method, endpoint, args, actor):            # ADM-011
    authorize console scope and requested A6 credential reference
    credential = resolve authorized credential from args.credential_id
    return S01.admin_console_command({method, endpoint}, args, credential)
    # S01 executes only versioned READ whitelist; managed mutations become owning S14 commands.
```

## 需求覆盖索引

本稿实现：`ADM-001`, `ADM-002`, `ADM-003`, `ADM-004`, `ADM-005`, `ADM-006`, `ADM-007`, `ADM-008`, `ADM-009`, `ADM-010`, `ADM-011`, `ADM-012`, `ADM-013`, `ADM-014`。
