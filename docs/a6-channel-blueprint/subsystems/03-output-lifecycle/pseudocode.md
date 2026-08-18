# S03 出口生命周期与入口 Key：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. 新建与 replacement

```text
plan_channel_apply(binding_id, mode):                         # LIF-001,002,004,012
    lock stable output channel
    generation = current desired/lifecycle generation(binding_id)
    require local user INACTIVE if first create
    require effective public text selections
    require staged ingress Key
    begin transaction
    create operation(identity=binding_id+generation+APPLY, phase=PRE_READ)
    S05.pause_current_providers_for_owned_change_in_tx(tx, credential,
        operation.identity, reason=OWNED_CHANNEL_CREATE_ADOPT)
    insert its S14 Outbox row in SAME tx
    commit

reconcile_channel_apply(op):                                  # LIF-002,004,011,013
    binding_id = op.channel_binding_id
    credential = op.credential
    resume phase journal
    require authoritative channel(binding_id).maintenance_state == NONE before planning replacement/apply
    inventory_before = S01.complete_supplier_inventory(credential)
    if op has no confirmed request:
        result = S01.execute_mutation(op child,
            exact supplier-request + complete-inventory pre-reader,
            POST supplier/apply using op.persisted_application_payload,
            exact supplier-request + complete-inventory post-reader,
            matcher for this operation's unique accepted/pending request identity)
        if result == REPLAY_SAME_OPERATION: return RETRYABLE
        if result in [READBACK_INCOMPLETE, RETRY_LATER]: return RETRYABLE
        if result == REMOTE_REJECTED: record visible non-retryable apply failure; return NON_RETRYABLE
        if result == REMOTE_DIVERGED: ensure BINDING_REVIEW with exact evidence; return NON_RETRYABLE
        if pending_request_exists: adopt matching pending request only if unique
    request = exact supplier-request read for op; require accepted/current for op
    inventory_after = S01.complete_supplier_inventory(credential)
    candidates = new channels owned by credential and matching selected corpus/config
    if candidates.count != 1: ensure BINDING_REVIEW; stop
    channel_readback = candidates.one.readback
    S00.replace_remote_instance(binding_id, candidates.one.id, channel_readback,
        followup_command=SYNC_LISTING_INVENTORY(binding_id, inventory_after.source_generation))
    sync_listing_inventory(binding_id, inventory_after)  # eager path; durable Outbox makes crash between steps recoverable
    # Listing adoption transactions themselves enqueue S05/S07/S09 gates; do not emit a second non-durable event here.
    gate = S05.reconcile_owned_channel_create_or_adopt(op.gate_child, candidates.one)
    if gate READY: enqueue S06 reactivation for affected Providers
    else: retain S05-owned pause; output activation may still use healthy Class2/3
```

## 2. Listing inventory

```text
adopt_listing_instance_in_tx(tx, channel_binding_id, stable_binding?, remote_listing, complete_inventory): # LIF-003,007,LIF-013
    require tx active and complete_inventory.complete and unique owner/channel/canonical-model match
    lock credential -> channel binding -> listing binding
    if stable_binding absent: create stable listing binding in tx
    result = S00.replace_remote_instance_in_tx(tx, binding_id, remote_listing.id, remote_listing.readback)
    if result.changed:
        if result.old exists and exact remote delete + complete current inventory prove absence:
            S05.exit_owned_listing_after_confirmed_remote_delete_in_tx(
                tx, result.old, exact delete evidence, complete_inventory)
        # Otherwise old local-retired/replaced/takedown instance remains in the exclusion set.
        S05.enter_listing_ban_domain_in_tx(tx, result.current)  # membership+bump+BAN_INIT_REQUIRED Outbox
        insert S07/S09 gates-required Outbox rows in SAME tx
    return result.current

adopt_listing_instance(channel_binding_id, stable_binding?, remote_listing, complete_inventory): # LIF-003,007,LIF-013
    begin transaction
    current = adopt_listing_instance_in_tx(tx, channel_binding_id, stable_binding,
                                           remote_listing, complete_inventory)
    commit
    return current

sync_listing_inventory(channel_binding_id, complete_inventory): # LIF-003,007
    require complete_inventory.complete
    for canonical model expected by local desired facts:
        matches = remote listings with exact owner/channel/canonical model
        if matches.count == 1:
            adopt_listing_instance(channel_binding_id, stable binding if any, matches.one, complete_inventory)
        else:
            ensure BINDING_REVIEW(source_generation, evidence)
    for current local instance absent remotely and not already replaced above:
        if absence proven and canonical parent/model unchanged:
            close admission; create replacement-listing operation through S14; keep old membership until retirement/replacement is proven
        else ensure BINDING_REVIEW
```

## 3. 激活

```text
try_activate_channel(binding_id, generation):                 # LIF-001
    require current Channel instance uniquely read back
    require inventory complete and intended Listings bound
    require each intended Listing LISTING_BAN_INIT READY
    require S07 output config/current prices confirmed
    require S09 required transport profiles eligible
    S02.activate_staged_output(binding_id, expected_generation=generation)
    # S02 rechecks/locks the same current gate vector and atomically inserts recovery Outbox rows.
```

## 3.1 Channel-scope maintenance primitive

```text
begin_channel_maintenance_in_tx(tx, channel, kind, op, remote_pre_read): # LIF-013
    require tx active; lock channel
    require kind in [KEY_ROTATION, BAN_INIT]
    pre = load persisted maintenance_pre_state for op/channel
    if absent: persist immutable {remote_pre_read, local pre-state} ONCE before any mutation
    set maintenance_state=kind and parent_channel_admission=CLOSED
    return persisted pre-state

begin_channel_maintenance(channel, kind, op):                 # LIF-013
    remote_pre = exact S01 Channel readback BEFORE first mutation
    begin transaction
    pre = begin_channel_maintenance_in_tx(tx, channel, kind, op, remote_pre)
    commit; return pre

temporary_restore_channel_for_ban_init(op, channel):          # LIF-013 (consumer: BAN-008)
    pre = begin_channel_maintenance(channel, BAN_INIT, op)
    if pre.remote_state == DOWN:
        resolution = S01.restore_supplier_channel(op child, channel)
        if resolution != CONFIRMED_ADOPT: return pre with restore=PENDING
    return pre with restore=CONFIRMED
    # Same S05 BAN_INIT operation already owns Provider pause/Ban; do not trigger nested EXCLUSION_GATE.

restore_channel_pre_ban_state(op, channel):                   # LIF-013 (consumer: BAN-008)
    pre = require persisted maintenance_pre_state(op, channel)
    resolution = S01 mutation/readback restoring exact pre.remote_state only if current differs
    if resolution != CONFIRMED_ADOPT: keep maintenance_state/admission CLOSED; return PENDING
    begin transaction
    require current remote readback == pre.remote_state
    clear maintenance_state for matching op; recompute parent admission from lifecycle gates
    commit; return CONFIRMED
```

## 4. 入口 Key 轮换 Saga

```text
execute_channel_state_mutation(op, channel, action, target_state): # LIF-004,005,008,009,013
    require channel maintenance/retirement operation is current
    resolution = S01.execute_mutation(op child, exact Channel pre-read,
                                     typed Channel action, exact Channel post-read,
                                     matcher for target_state)
    if resolution == CONFIRMED_ADOPT: return CONFIRMED
    if resolution == REPLAY_SAME_OPERATION: return RETRY_SAME_ID
    if resolution in [READBACK_INCOMPLETE, RETRY_LATER]: return PENDING
    if resolution == REMOTE_DIVERGED: return REPLAN_OR_REVIEW
    if resolution == REMOTE_REJECTED: return NON_RETRYABLE

start_ingress_key_rotation(command_op, channel_id):            # LIF-008,011,012,013
    channel = load authoritative stable Channel by channel_id
    require command_op is the current S14 owner command for channel/current lifecycle generation
    selection_generation = stable child generation derived once from command_op.identity
    selection = S00.select_public_text(channel, INGRESS_KEY_NAME, selection_generation,
        mode=RANDOM, followup_command=RESUME_KEY_ROTATION(command_op.id))
    require selection valid; replay of command_op must reuse this exact selection_generation/value
    remote_pre = exact S01 Channel readback(channel.current_instance) before any rotation mutation
    begin transaction
    lock channel; require command_op/source generation still current
    new_key = idempotently create/load one DISABLED_STAGED Sub2 key keyed by command_op.identity
        through existing transaction-aware Sub2 key service(name=selection.value)
    operation = idempotently create/load parent operation(
        identity=command_op.identity, channel_id, old_key=current effective key, new_key, phase=STAGE_NEW_KEY)
    begin_channel_maintenance_in_tx(tx, channel, KEY_ROTATION, operation, remote_pre)
    insert/verify key-rotation operation Outbox in SAME tx
    commit

reconcile_key_rotation(op):                                   # LIF-008..011
    channel = authoritative stable Channel(op.channel_id)
    binding_id = channel.binding_id
    old_key = require op.old_key_ref
    new_key = require op.new_key_ref
    lock channel; resume journal
    phase STAGE_NEW_KEY:
        require new key staged and corpus selection persisted
        close parent Channel admission; advance CHANNEL_TAKEDOWN
    phase CHANNEL_TAKEDOWN:
        outcome = execute_channel_state_mutation(op, channel, TAKEDOWN, DOWN)
        if outcome != CONFIRMED: keep maintenance/admission CLOSED; return outcome
        advance UPDATE_REMOTE_KEY
    phase UPDATE_REMOTE_KEY:
        with S00.with_secret(new_key.secret_ref, CHANNEL_KEY_ROTATION) as new_secret:
            resolution = S01.update_supplier_channel(op, binding_id, api_key=new_secret)
        if CONFIRMED_ADOPT: advance SWITCH_LOCAL_KEY
        if REPLAY_SAME_OPERATION: replay UPDATE_REMOTE_KEY under same op
        if REMOTE_DIVERGED: resolve_key_side(op) from exact readback
        if READBACK_INCOMPLETE/RETRY_LATER: remain CLOSED and retry readback
        if REMOTE_REJECTED: remain CLOSED; visible operation failure, no blind replay
    phase SWITCH_LOCAL_KEY:
        begin transaction
        disable old key; enable new key; set exactly one effective via constraint
        commit
        advance VERIFY_REMOTE_KEY
    phase VERIFY_REMOTE_KEY:
        require remote config uniquely points to new key generation
        S05.pause_current_providers_for_owned_change(
            op, channel.credential, reason=OWNED_CHANNEL_RESTORE)
        advance CHANNEL_RESTORE
    phase CHANNEL_RESTORE:
        resolution = S01.restore_supplier_channel(op, binding_id)
        if resolution != CONFIRMED_ADOPT:
            keep maintenance/admission CLOSED; resolve/retry according to S01 classification; return
        begin transaction
        require exact Channel UP/readback and matching new key generation
        clear channel maintenance_state for this op
        insert child observer/admission reprojection Outbox rows in SAME tx
        insert S05 OWNED_CHANNEL_RESTORE_GATE Outbox in SAME tx
        commit                                  # no child Listing resubmit/takedown mutation
        gate = S05.run_owned_exclusion_gate(op.restore_gate_child,
            all Ban-domain tokens, channel models,
            require_effective_subscription for current Provider tokens only)
        if gate READY: enqueue S06 Provider reactivation
        else: keep affected Providers paused; do not roll back confirmed Key/Channel restore
        advance DELETE_OLD_LOCAL_KEY
    phase DELETE_OLD_LOCAL_KEY:
        delete old disabled local key
        on failure record cleanup dead-letter without changing new effective key
        complete

resolve_key_side(op):                                         # LIF-009
    channel = authoritative stable Channel(op.channel_id)
    old_key = require op.old_key_ref; new_key = require op.new_key_ref
    remote = exact Channel config readback(channel.current_instance)
    if remote uniquely matches old_key fingerprint/generation:
        transactionally ensure old_key only enabled and new_key disabled; set op.phase=UPDATE_REMOTE_KEY
    else if remote uniquely matches new_key fingerprint/generation:
        set op.phase=SWITCH_LOCAL_KEY or VERIFY_REMOTE_KEY according to local effective key
    else:
        ensure Channel DOWN and all child admission CLOSED
        retain operation as ambiguous; never enable both
```

## 5. 退役

```text
retire_listing(binding_id, op):                               # LIF-005,006
    close admission and set RETIRE_PENDING
    wait asynchronously until inflight(binding_id)==0
    if leases stale: reconcile attempts; never assume zero from timeout alone
    before remote write operator may cancel and reopen only if gates pass
    after takedown attempt starts cancellation forbidden
    S13 executes takedown/readback under current operation
    begin transaction
    close current instance and mark binding RETIRED
    retain current_instance in S05 authoritative exclusion set
    insert required follow-up Outbox rows
    commit
    if no non-retired siblings: retire_parent_channel(op.parent)

retire_parent_channel(op):                               # LIF-005,LIF-006
    channel = authoritative stable Channel(op.parent_channel_id)
    require every Listing retired and no inflight
    outcome = execute_channel_state_mutation(op, channel, TAKEDOWN, DOWN)
    if outcome != CONFIRMED: keep retirement pending; return outcome
    begin transaction
    disable current ingress key, retire user and close current Channel instance
    retain Channel/Listings in S05 authoritative exclusion set until exact remote delete + complete absence proof
    insert retirement/audit follow-up Outbox in SAME tx
    commit; return CONFIRMED
```

## 需求覆盖索引

本稿实现：`LIF-001`, `LIF-002`, `LIF-003`, `LIF-004`, `LIF-005`, `LIF-006`, `LIF-007`, `LIF-008`, `LIF-009`, `LIF-010`, `LIF-011`, `LIF-012`, `LIF-013`。
