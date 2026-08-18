# S01 A6 Typed Connector：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. 核心类型

```text
type CredentialRef { a6_user_id, secret_generation_id }
type ReadResult<T> { value:T, complete:bool, source_hash, fetched_at, pages, extensions }
type MutationContext { operation_id, idempotency_key, actor, department, source_generation }
type MutationResolution = CONFIRMED_ADOPT | REPLAY_SAME_OPERATION |
                          REMOTE_DIVERGED | REMOTE_REJECTED |
                          READBACK_INCOMPLETE | RETRY_LATER
type A6ErrorEnvelope { http_status, code, message, request_id, retry_after,
                       transport_outcome, redacted_hash, extensions }
```

## 2. 统一调用

```text
call_typed(method, path, query, body, credential_ref, semantics):   # CON-001..003,008
    validate request against endpoint registry
    with S00.with_secret(credential_ref, A6_CONTROL_PLANE) as token:
        headers = {Authorization: "Bearer " + token,
                   "New-API-User": credential_ref.a6_user_id}
        response = transport.send(method, path, query, typed_encode(body), headers)
    parsed = typed_decode(response, preserve_unknown=true, decimal_from_wire=true)
    audit only redacted metadata and response hash
    return parsed or A6ErrorEnvelope

read(endpoint, args, credential):                                # CON-007
    retry boundedly only if registry[endpoint].semantics == IDEMPOTENT_READ
    if status == 429: return RETRY_LATER(retry_after, last_confirmed_unchanged=true)
    never generic-retry generate_system_access_token
```

## 3. 完整分页

```text
collect_complete(endpoint, args, credential):                    # CON-004
    items = []
    seen_page_tokens = set()
    completeness = true
    for page in paginator(endpoint):
        result = read(endpoint, args + page, credential)
        if result failed: return ReadResult(items, complete=false, failure=result)
        if page.token in seen_page_tokens: completeness = false; break
        record page raw hash and declared total
        append items without hiding duplicate IDs
        if endpoint == circuit_states and count(page.items) == 1000:
            completeness = false
        if page metadata contradicts previous total/order:
            completeness = false
        if paginator says final: break
    validate unique resource identities where endpoint requires them
    return ReadResult(items, completeness, combined_hash, now, pages)

prove_absent(read_result, predicate):                            # CON-004
    require read_result.complete
    require no duplicate/ambiguity affecting predicate
    return none(read_result.items, predicate)
```

## 4. Mutation 与结果不明

```text
execute_mutation(ctx, pre_reader, mutation, post_reader, matcher=registry matcher for mutation): # CON-005,006
    pre = pre_reader()
    require pre.complete_for_contract
    persist audit(ctx, phase=PRE_READ, hash=pre.source_hash)
    try:
        wire = mutation(ctx.idempotency_key)
        persist redacted wire outcome
    catch transport_timeout_or_reset:
        wire = UNKNOWN_TRANSPORT_RESULT
    post = post_reader()
    persist audit(ctx, phase=POST_READ, hash=post.source_hash, complete=post.complete)
    if not post.complete_for_contract: return READBACK_INCOMPLETE
    classification = matcher(pre.value, post.value)
    if classification == TARGET: return CONFIRMED_ADOPT
    if wire is definitive business/non-retry rejection:
        return REMOTE_REJECTED(wire.error, post.value)
    if classification == EXACT_PRE_STATE and wire is UNKNOWN_TRANSPORT_RESULT:
        return REPLAY_SAME_OPERATION
    if classification == EXACT_PRE_STATE:
        return REMOTE_REJECTED(wire.error_or_non_success, post.value)
    return REMOTE_DIVERGED(post.value)
```

Owning reconciler 对 `REPLAY_SAME_OPERATION` 复用 `operation_id/idempotency_key`；对 `REMOTE_DIVERGED` 生成新 plan generation。

## 5. 特殊合同

```text
delete_token(ctx, token_id):                                    # CON-011
    resolution = execute_mutation(ctx, exact token pre-read, DELETE /api/token/{id},
                                  detail+complete-list post-reader, deletion matcher)
    if resolution == CONFIRMED_ADOPT:
        require detail == record_not_found and complete list proves ID absent
    # exact-pre may become REPLAY only when the DELETE transport result was unknown;
    # definitive reject stays REMOTE_REJECTED by CON-006.
    return resolution

update_supplier_channel(ctx, binding_id, fields):                # CON-013
    typed body allows nickname, description, models, base_url,
        test_model and api_key; api_key never logged
    return execute_mutation(ctx, exact channel pre-read, PATCH, exact channel post-read,
                            matcher for writable fields/key fingerprint)
    # preserve CONFIRMED_ADOPT / REPLAY_SAME_OPERATION / REMOTE_DIVERGED /
    # REMOTE_REJECTED / READBACK_INCOMPLETE / RETRY_LATER

restore_supplier_channel(ctx, binding_id):                       # CON-014
    typed body is exactly {}
    return execute_mutation(ctx, exact channel pre-read,
                            POST /api/supplier/channels/{binding_id}/restore,
                            supplier-channel post-reader, restore matcher)
    # Marketplace readback is added by the owning caller only when its business gate needs it.

update_supplier_profile(ctx, contact_info, description):         # CON-010
    typed body has exactly contact_info and description
    reject field name or rename intent at compile/API validation boundary
    PATCH then GET /api/supplier/me and compare confirmed writable fields

complete_owned_market_search(token, model):                       # CON-004
    credential = authoritative A6 credential owning token
    args = {token_id:token.id, model, route_status:"all", include_seed:true,
            include_disabled:true, include_user_disabled:true, exclude_unavailable:false}
    return collect_complete(marketplace_channels_search, args, credential)

complete_supplier_inventory(credential):                          # CON-004
    channels = collect_complete(supplier_channels, {}, credential)
    listings = collect_complete(supplier_listings, {}, credential)
    return combined inventory with complete=(channels.complete and listings.complete),
           both source hashes/pages and raw items; never infer absence if either incomplete

batch_markup_preview(channel_ids, credential):                    # CON-003,007
    require channel_ids are explicit stable remote IDs
    return typed read through endpoint registry for the confirmed batch-markup preview contract

disable_token_for_listing(ctx, token, listing):                   # CON-005,006
    return execute_mutation(ctx,
        exact Token×Listing disable-state pre-reader,
        registered user-disable mutation for token+listing,
        exact Token×Listing disable-state post-reader,
        matcher requiring the contract's user-disabled fields)

admin_console_command(endpoint, args, credential):                # CON-009
    require endpoint+method in versioned read whitelist
    if registry says mutation: create S14 command; do not call transport
    else return read(endpoint, args, credential)
```

## 6. 覆盖自检

```text
assert_reference_coverage():                                    # CON-002
    confirmed = load_machine_readable_endpoint_manifest_checked_into_repo
    implemented = endpoint_registry.keys
    require confirmed - implemented == empty
    require every mutation has pre_reader, post_reader, matcher and redaction policy
```

## 需求覆盖索引

本稿实现：`CON-001`, `CON-002`, `CON-003`, `CON-004`, `CON-005`, `CON-006`, `CON-007`, `CON-008`, `CON-009`, `CON-010`, `CON-011`, `CON-012`, `CON-013`, `CON-014`。
