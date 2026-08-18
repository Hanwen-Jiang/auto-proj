# S02 出口身份、用户与绑定：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Provision 本地身份

```text
stage_output_identity(command):                              # OUT-001,002,011
    begin transaction
    group = require_exact_canonical_group("ToA6API")
    user = insert users(user_type="a6-channel", status=INACTIVE,
                        balance=Decimal("0"), concurrency=0, quota=0)
    insert exact ToA6API membership(user, group_multiplier=Decimal("1.0"))
    require user has exactly group and multipliers == Decimal("1.0")
    channel = insert stable a6_output_channel(local_user_id=user.id)
    key = create_sub2_key(user.id, state=DISABLED_STAGED,
                          name_selection_ref=required_corpus_selection)
    insert output_channel_key(channel.id, key.id, effective=false)
    insert staged-identity follow-up Outbox in SAME tx
    commit
```

## 2. 鉴权与资源限制

```text
authenticate_output_request(raw_key, request):               # OUT-004..006
    auth = normal_key_auth(raw_key)
    require auth.user.user_type == "a6-channel"
    require auth.key == effective_key_for(auth.user.channel)
    require auth.user.status == ACTIVE
    require exact_group(auth.user) == ToA6API and multipliers == 1.0
    enforce_key_ip_rpm_limits(auth, request)
    enforce_concurrency_unless_zero(auth.user.concurrency)
    enforce_user_quota_unless_zero(auth.user.quota)
    # Do not call balance reserve or platform-level quota accounting for this user type.
    return auth

normal_user_request(request, auth_context):               # OUT-004..006
    require user_type != "a6-channel" for ordinary groups
    preserve existing balance/reserve/platform quota behavior
```

## 3. 模型解析

```text
resolve_listing(local_user_id, requested_model, alias_generation): # OUT-007,010
    exact = query current listing binding where
            local_user_id and normalized_model == requested_model
    if exactly one: return exact
    aliases = query enabled versioned aliases matching requested_model
    if alias_generation not current or aliases count != 1:
        ensure BINDING_REVIEW command for collision/ambiguity
        reject before scheduler
    binding = current listing for aliases[0].canonical_model
    require exactly one
    return binding

build_dispatch_context(auth, request):                       # OUT-010
    binding = resolve_listing(auth.user.id, request.model, current_alias_generation)
    require binding parent current and listing admission OPEN
    require parent Channel admission OPEN and maintenance_state == NONE
    profile = normalize_transport_profile(request.endpoint, request.stream,
                                          expected_terminal_usage_schema)
    return {local_user_id, listing_binding_id, normalized_model,
            output_channel_generation, listing_generation, profile}
```

## 4. Models 端点

```text
list_output_models(auth):                                    # OUT-008
    require auth.user_type == "a6-channel"
    return current listing bindings where
        parent channel ACTIVE and admission OPEN and
        current model/capability/pricing generations confirmed
    serialize only canonical A6 model_no and allowed metadata
```

## 5. 候选投影

```text
project_output_candidates(listing_binding, generation):      # OUT-009
    accounts = explicit account bindings for ToA6API and model
    for account in accounts:
        join current S04 economic/capability facts
        join current S05 BAN_INIT READY when Class1
        join S09 eligibility row
    write Redis projection atomically with generation/hash
    never read or write group multiplier as economic input
```

## 6. 激活门

```text
activate_staged_output(channel_id, expected_generation):      # OUT-011
    begin transaction
    lock credential -> user/channel -> all intended Listing rows/current gate pointers in canonical order
    require channel lifecycle/source generation == expected_generation and maintenance_state == NONE
    require S03 binding/key gates confirmed under these locks
    require all relevant S05 BAN_INIT READY for the current Token/Listing matrix and current membership generation
    require S07 current price/config generations confirmed
    require S09 each intended Listing has required-profile candidates for the same current gate vector
    gate_hash = hash(binding,key,ban_membership,price/config,eligibility generations)
    require gate_hash unchanged immediately before commit
    require exactly one staged/effective transition plan
    enable key and user atomically; mark key effective; persist activation gate_hash
    insert per-Listing S13 recovery Outbox rows in the SAME transaction
    commit
    publish optional wake-up notification after commit
```

## 7. Group drift

```text
audit_toa6api_group():                                       # OUT-003,012
    require exactly one group named ToA6API
    require no TPA6API runtime alias/entity after migration cutover
    require every member user_type == a6-channel
    require every a6-channel belongs only to this group
    require group and user-group multipliers == Decimal("1.0")
    on drift:
        begin transaction
        close affected admission with current drift generation
        insert config-invalid owner/reprojection Outbox in SAME tx
        commit
```

## 需求覆盖索引

本稿实现：`OUT-001`, `OUT-002`, `OUT-003`, `OUT-004`, `OUT-005`, `OUT-006`, `OUT-007`, `OUT-008`, `OUT-009`, `OUT-010`, `OUT-011`, `OUT-012`。
