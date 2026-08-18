# S05 Provider Token、Ban Initialization 与 Self-Route Guard：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Owned 清单维护

```text
build_owned_routing_listings(credential):                     # BAN-005
    supplier = S01.complete_supplier_inventory(credential)
    require supplier.complete
    remote = every owned Channel/Listing still present in supplier,
             including active/down/unavailable/retired/hidden
    local = every locally controlled Channel/Listing without exact remote-delete proof
    return union(remote, local) with supplier source generation/evidence

build_owned_historical_tombstones(credential):                # BAN-005
    query instances with exact remote-delete confirmation and
        complete current Supplier inventory proving absence
    return tombstone set; audit only, never used for Ban init/gates
```

## 1.1 Ban domain membership transitions（generation 的唯一推动点）

```text
# 成员转换与 generation bump 是一个不可分割的 DB transaction。
# worker crash/replay 只能看到“全部未提交”或“membership+bump+audit 全部已提交”。

transition_ban_membership_once(tx, credential, member, action, reason): # BAN-004,005,FND-014
    require tx active; lock credential
    changed = idempotently apply exactly one transition in tx:
        TOKEN ENTER -> insert Ban-domain membership if absent
        TOKEN EXIT  -> mark permanently exited only after remote delete confirmed
        LISTING ENTER -> insert owned_routing membership if absent
        LISTING EXIT  -> move owned_routing -> historical tombstone only after
                         exact remote delete + complete current Supplier absence
    if not changed: return NO_TRANSITION, current ban_membership_generation
    new_generation = S00.advance_ban_membership_generation_in_tx(tx, credential, reason)
    append membership transition evidence in same tx
    if action == ENTER:
        ban_reason = NEW_PROVIDER_TOKEN if member is Token else NEW_OWNED_ROUTING_LISTING
        insert deduped BAN_INIT_REQUIRED Outbox(member, new_generation, ban_reason) in SAME tx
    return TRANSITION_OCCURRED, new_generation

enter_token_ban_domain_in_tx(tx, token_instance):             # BAN-004,FND-014
    credential = authoritative credential owning token_instance
    return transition_ban_membership_once(tx, credential, token_instance, ENTER,
                                          PROVIDER_TOKEN_INSTANCE_ADDED)

enter_listing_ban_domain_in_tx(tx, listing_instance):         # BAN-004,005,FND-014
    credential = authoritative credential owning listing_instance's parent Channel
    return transition_ban_membership_once(tx, credential, listing_instance, ENTER,
                                          OWNED_ROUTING_LISTING_ADDED)

enter_token_ban_domain(token_instance):                       # BAN-004,FND-014
    begin transaction
    result = enter_token_ban_domain_in_tx(tx, token_instance)
    commit; return result

enter_listing_ban_domain(listing_instance):                   # BAN-004,005,FND-014
    begin transaction
    result = enter_listing_ban_domain_in_tx(tx, listing_instance)
    commit; return result

exit_token_ban_domain_after_confirmed_delete(token_instance): # BAN-003,004,FND-014
    credential = authoritative credential owning token_instance
    begin transaction
    result = transition_ban_membership_once(tx, credential, token_instance, EXIT,
                                            PROVIDER_TOKEN_INSTANCE_REMOVED)
    commit; return result

exit_owned_listing_after_confirmed_remote_delete_in_tx(
        tx, listing_instance, delete_readback, complete_inventory): # BAN-005,FND-014
    require delete_readback exact and complete_inventory.complete
    require listing/channel absent from complete_inventory
    credential = authoritative credential owning listing_instance's parent Channel
    return transition_ban_membership_once(tx, credential, listing_instance, EXIT,
                                          OWNED_ROUTING_LISTING_REMOTE_DELETE_CONFIRMED)

# price/route/max_ratio/Pin/上下架/cooling/local retirement/EXCLUSION_GATE
# 均不得调用 membership transition。
```

## 2. BanRelation 与 BAN_INIT

```text
create_missing_ban_relations(op, pairs):                      # BAN-004
    begin transaction
    for token, listing_instance in stable unique pairs:
        row = load relation(token, listing_instance) FOR UPDATE
        if absent:
            insert relation(state=PENDING, operation_id=op.id)
        else if row.state == READY:
            keep READY                                      # never downgrade/re-ban
        else if row.state == FAILED and row.operation_id == op.id:
            keep FAILED                                     # definitive failure of this op is sticky
        else if row.state == FAILED:
            require op explicitly authorizes retry/supersession of row.operation_id under current generation
            set PENDING, operation_id=op.id, clear current failure fields
        else:
            keep PENDING and require row.operation_id == op.id or explicitly adopt/supersede it
    require unique (token, listing) rows
    current = load relation rows for requested pairs
    commit
    return current

run_ban_init(op, initial_tokens, initial_listings, mode):      # BAN-006..008
    # One matrix reconciler for TOKEN/LISTING/REPAIR. Visibility is grouped by Listing,
    # so a Listing is temporarily resubmitted/restored at most once per reconciliation pass,
    # not once per Token.
    credential = op.credential
    pending_tokens = stable set(initial_tokens)
    pending_listings = stable set(initial_listings)
    while true:
        membership_start = current_ban_membership_generation(credential)
        # First repair any maintenance residue left by a crash before deciding READY rows are done.
        recover unresolved Channel/Listing maintenance_pre_state owned by op
        if any restore not confirmed: return RETRYABLE

        pairs = missing BanRelations for pending_tokens × pending_listings
        # Before completion, also absorb any missing relation in the CURRENT Ban-domain matrix.
        pairs += current_matrix_missing_relations(credential)
        relations = create_missing_ban_relations(op, pairs)
        if any relation FAILED with operation_id == op.id:
            require no unresolved maintenance state for op
            return NON_RETRYABLE

        pass_outcome = READY
        cleanup_pending = false
        groups = group pair Listings by parent_channel stable ID
        for channel, listing_pairs in groups while pass_outcome == READY:
            channel_pre = S03.temporary_restore_channel_for_ban_init(op, channel)
            if channel_pre.restore != CONFIRMED: pass_outcome = RETRYABLE
            try:
                for listing in stable unique Listings from listing_pairs while pass_outcome == READY:
                    listing_pre = S13.persist_ban_pre_state_once(listing, op)
                    relation_tokens = tokens with a missing relation for this listing
                    temporary_used = false
                    try:
                        if listing_pre requires temporary resubmit and relation_tokens not empty:
                            temp = S13.temporary_list_for_ban_init(listing, op)
                            if temp != CONFIRMED: pass_outcome = RETRYABLE; continue
                            temporary_used = true
                        for token in relation_tokens while pass_outcome == READY:
                            ban_state = exact S01 Ban relation readback(token, listing)
                            if already exact user-disabled: mark relation READY; continue
                            resolution = S01.disable_token_for_listing(op child, token, listing)
                            if resolution == CONFIRMED_ADOPT:
                                mark relation READY
                            else if resolution in [REPLAY_SAME_OPERATION, READBACK_INCOMPLETE, RETRY_LATER]:
                                keep relation PENDING with resolution/evidence
                                pass_outcome = RETRYABLE
                            else: # REMOTE_REJECTED or REMOTE_DIVERGED
                                mark relation FAILED for this operation with exact evidence
                                pass_outcome = NON_RETRYABLE
                    finally:
                        if temporary_used or S13.ban_override_active(listing, op):
                            if S13.restore_pre_ban_state(listing, op) != CONFIRMED:
                                cleanup_pending = true
            finally:
                if S03.restore_channel_pre_ban_state(op, channel) != CONFIRMED:
                    cleanup_pending = true

        if cleanup_pending:
            persist underlying pass_outcome/evidence on op; return RETRYABLE_CLEANUP
        if pass_outcome != READY: return pass_outcome  # cleanup is proven restored before terminal failure; no busy-loop

        membership_end = current_ban_membership_generation(credential)
        full_missing = current_matrix_missing_relations(credential)
        if membership_end == membership_start and full_missing empty and no unresolved maintenance state for op:
            require every current required relation READY
            return READY(generation=membership_end)
        pending_tokens = current_ban_domain_tokens(credential)
        pending_listings = current_owned_routing_listings(credential)
        # membership changed while we worked; loop only to close the new missing matrix. READY rows are untouched.

# Auto-Pool 无交集校验（BanRelation 仍是 Token × Listing，
# 但 auto_channels 本身是 Channel ID，因此投影回 parent Channel）：
owned_channel_ids =
    unique(
        listing.parent_remote_channel_id
        for listing in build_owned_routing_listings(credential)
    )
route = exact subscription readback
require route.auto_channels non-empty
    require intersection(set(route.auto_channels), owned_channel_ids).empty
```

## 2.1 事件驱动 owned-Channel exclusion gate

```text
run_owned_exclusion_gate(op, tokens, models, require_effective_subscription): # BAN-004..011
    require op.trigger in [TOKEN_ONBOARD, OWNED_CHANNEL_CREATE_ADOPT, OWNED_CHANNEL_RESTORE,
                           PROVIDER_SCHEDULABLE, MAX_RATIO_UPDATE, SELF_ROUTE_REPAIR]
    credential = op.credential
    pause affected current Providers before remote reads; staged Tokens remain unpublished
    membership_start = current_ban_membership_generation(credential)
    owned = build_owned_routing_listings(credential)          # exactly one complete Supplier inventory
    pairs = every stable token × owned Listing whose canonical model is in models
    relations = create_missing_ban_relations(op, pairs)

    for token, model in stable unique token/model pairs:
        market = S01.complete_owned_market_search(token, model)
        if not market.complete: return BLOCKED(INCOMPLETE_MARKETPLACE)
        for listing in owned filtered to model:
            item = unique exact market item for listing parent Channel/model
            if item absent or non-unique:
                return BLOCKED(OWNED_ITEM_MISSING_OR_AMBIGUOUS)
            relation = relation(token, listing)
            if item.user_channel_disabled == true and item.route_status == user_disabled:
                mark/adopt relation READY with this operation's fresh readback reference
                continue                                           # zero disable mutation
            result = S01.disable_token_for_listing(op child, token, listing)
            if result != CONFIRMED_ADOPT: return BLOCKED(result)
            exact = S01 exact post-read for the same market item
            if exact.user_channel_disabled != true or exact.route_status != user_disabled:
                return BLOCKED(DISABLE_READBACK_MISMATCH)
            mark relation READY with this operation's exact readback reference

        if require_effective_subscription:
            route = S01.exact_effective_subscription(token, model)
            require route.complete and route.auto_channels non-empty
            require intersection(route.auto_channels, owned parent Channel IDs).empty
            if route.mode == PIN: require exact assignment Channel not owned

    if current_ban_membership_generation(credential) != membership_start:
        return RETRY_CURRENT_GENERATION
    append compact gate completion evidence to existing operation journal:
        trigger, membership_start, supplier generation, marketplace page refs,
        relation readback refs, optional subscription readback refs
    return READY(generation=membership_start, operation_id=op.id)

# No gate table, no gate lifecycle state, no token-set/owned-set hash.
# Consumers may only use READY inside the owning operation and while generation remains current.
```

## 3. Provider Token 轮换

```text
start_provider_token_rotation(account, command_op):           # BAN-001,002,012
    begin transaction
    lock account and current token binding
    require command_op is current for account/source generation
    require valid persisted corpus token name selection bound to command_op identity/generation
    operation = idempotently create/load rotation operation(identity=command_op.identity, phase=CREATE_NEW_TOKEN)
    insert/verify rotation Outbox(operation identity) in SAME tx
    commit; return operation

reconcile_provider_token_rotation(op):                        # BAN-002,003
    phase CREATE_NEW_TOKEN:
        resolution = S01 token create/reveal mutation under this stable operation identity + exact get/list readback
        if resolution != CONFIRMED_ADOPT: resolve/retry/review by S01 classification; return
        store revealed secret via S00 and persist/adopt exactly one new Token instance
        enter_token_ban_domain(new token)                     # atomic membership+bump+BAN_INIT_REQUIRED Outbox
        advance BAN_INIT_NEW_TOKEN
    phase BAN_INIT_NEW_TOKEN:
        owned = build_owned_routing_listings(op.credential)
        ban_ready = run_ban_init(op, [new token], owned, mode=TOKEN)
        if ban_ready not READY: return ban_ready
        gate = run_owned_exclusion_gate(op.gate_child, [new token], supported models,
                                        require_effective_subscription=false)
        if gate not READY: return gate
        persist gate generation as COPY_ROUTE fence
        advance COPY_ROUTE
    phase COPY_ROUTE:
        old_route = read old models/quota/route
        route_result = S06.configure_route_for_staged_token(
            op, account, new token, old_route, pre_route_gate=gate)
        require route_result confirmed and owned by S06
        require exact Auto-Pool subscription is non-empty/disjoint, or exact Pin is non-owned
        require matching S04 cost/capability generations
        persist gate generation as SWITCH_BINDING fence
        require route/cost/model/callability current and exact
        advance SWITCH_BINDING
    phase SWITCH_BINDING:
        begin transaction
        lock credential then account; require old binding still current
        if current_ban_membership_generation(op.credential) != op.ban_ready_generation
           or current_matrix_missing_relations(op.credential) not empty:
            keep old binding current; set phase=BAN_INIT_NEW_TOKEN; commit; return RETRYABLE
        require new token has every current required relation READY,
                onboarding gate generation still current, and route/cost/model/callability generations still current
        point account to new token generation
        insert schedulability/eligibility projection refresh Outbox in SAME tx
        commit; advance DELETE_OLD
    phase DELETE_OLD:
        result = S01.delete_token(op child, old token)
        if result == CONFIRMED_ADOPT:
            exit_token_ban_domain_after_confirmed_delete(old token) # atomic membership+bump
            complete
        else:
            mark old token ORPHANED_REMOTE_TOKEN with exact resolution/evidence
            keep it in Ban domain; retry only when S01/S14 classification permits; never restore as candidate

compensate_before_switch(op):                                 # BAN-003
    if any phase before SWITCH_BINDING irrecoverably fails:
        result = S01.delete_token(op cleanup child, new token)
        if result == CONFIRMED_ADOPT:
            # 只有远端删除确认后才把 Token 移出 Ban domain；
            # 否则远端 Token 可能实际还存在，却不再参与新 Listing BAN_INIT
            exit_token_ban_domain_after_confirmed_delete(new token) # atomic membership+bump
            keep old binding and schedulability unchanged
            complete compensation
        else:
            # DELETE unknown/failed：不清除本地 membership，不产生 REMOVED
            mark new token ORPHANED_REMOTE_TOKEN
            keep it in Ban domain
            keep old binding current
            retry cleanup under same operation
```

## 4. Listing 初始化与 SelfRoute 修复

```text
initialize_listing(op, listing_binding, generation):           # BAN-007
    credential = authoritative credential owning listing_binding's parent Channel
    listing = current listing instance for listing_binding/generation
    require op.kind == LISTING_BAN_INIT and op.credential == credential
    lock credential -> output Channel -> Listing
    require listing not yet handed to normal controllers
    enter_listing_ban_domain(listing)                         # idempotent atomic membership+bump
    membership_start = current_ban_membership_generation(credential)
    official = S07 current confirmed official price + FX generation
    init_ck = persisted_random_decimal(op.id, min="0.8", max="1.9") # one-shot, replay-reused
    init_vector = S07.target_listing_vector(official.vector, init_ck, official.fx)
    price_resolution = S07.apply_price_vector(op.init_price_child, init_vector)
    require price_resolution == CONFIRMED_ADOPT and exact S07 confirmed price generation
    tokens = current_ban_domain_tokens(credential)            # includes staged, non-current
    ban_ready = run_ban_init(op, tokens, [listing], mode=LISTING)
    if ban_ready not READY: return ban_ready
    require ban_ready.generation >= membership_start
    begin transaction
    lock credential -> output Channel -> Listing
    require listing still current for binding/generation
    require current_ban_membership_generation(credential) == ban_ready.generation
    require current_matrix_missing_relations(credential) empty
    mark LISTING_BAN_INIT READY for current listing instance/generation
    insert normal S08/S09/S13 handoff Outbox rows in SAME tx
    commit

reconcile_owned_channel_create_or_adopt(op, channel):          # BAN-007,009
    require S03 completed the Channel inventory batch and every intended LISTING_BAN_INIT READY
    tokens = current_ban_domain_tokens(op.credential)
    models = canonical models on the Channel
    return run_owned_exclusion_gate(op, tokens, models,
                                    require_effective_subscription=true)
    # One full gate per Channel batch, not one per Listing.

detect_self_route():                                          # BAN-010
    monitor route usage evidence for each current Provider Token
    if Token actually selected any owned_routing_listing:
        begin transaction
        pause affected Provider with generation fence
        insert deduped SELF_ROUTE_DETECTED Outbox(token, listing, evidence, current generation) in SAME tx
        commit

repair_ban_matrix(token?, listing?, evidence):                # BAN-011
    credential = authoritative credential from known token/listing/evidence scope
    tokens = [token] if token known else current_ban_domain_tokens(credential)
    listings = [listing] if listing known else current_owned_routing_listings(credential)
    repair_op = create/current SELF_ROUTE_REPAIR operation for evidence scope
    ban_ready = run_ban_init(repair_op, tokens, listings, mode=REPAIR)
    if ban_ready not READY: return ban_ready
    gate = run_owned_exclusion_gate(repair_op.gate_child, tokens,
                                    models(listings), require_effective_subscription=true)
    if gate not READY: return gate
    begin transaction
    lock credential then affected Provider/account rows
    require current_ban_membership_generation(credential) == gate.generation
    require current_matrix_missing_relations(credential) empty
    require route/cost/capability generations still current and no other pause/hold reason
    clear only SelfRoute-owned pause + insert schedulability refresh Outbox in SAME tx
    commit

current_ban_membership_generation(credential):                  # BAN-004,FND-014
    return S00.read_ban_membership_generation(credential)

current_ban_domain_tokens(credential):                         # BAN-004,005
    return all Token instances for credential whose Ban-domain membership is entered and not permanently exited

current_owned_routing_listings(credential):                    # BAN-005
    return build_owned_routing_listings(credential)

current_matrix_missing_relations(credential):                  # BAN-004,006,007
    tokens = current_ban_domain_tokens(credential)
    listings = current_owned_routing_listings(credential)
    return every pair in tokens × listings whose BanRelation is absent or state != READY
```

## 需求覆盖索引

本稿实现：`BAN-001`, `BAN-002`, `BAN-003`, `BAN-004`, `BAN-005`, `BAN-006`, `BAN-007`, `BAN-008`, `BAN-009`, `BAN-010`, `BAN-011`, `BAN-012`。
