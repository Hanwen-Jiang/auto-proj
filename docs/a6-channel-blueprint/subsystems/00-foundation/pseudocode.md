# S00 基础数据、安全与公共文本：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. 数值原语

```text
type Money = Decimal
type Ratio = Decimal
type Micros = int64_or_bigint

decimal_from_wire(value):                         # FND-003
    require value is decimal string, integer, or DB numeric
    reject binary float for economic input
    return Decimal(value)

ceil_scale(value, scale):                         # FND-003
    return value.quantize(10^-scale, ROUND_CEILING)

micros_ceil(value):                                      # FND-003
    return integer(value.quantize(1, ROUND_CEILING))

normalize_sk(raw):                                # FND-010
    material = trim(raw)
    while material starts_with "sk-": material = material[3:]
    require material != ""
    return "sk-" + material
```

所有业务函数签名使用 `Money/Ratio/Micros`，展示转换必须命名为 `to_display_float` 并位于序列化边界。

## 2. Binding/instance 原语

```text
create_stable_binding(owner_id, normalized_model?):       # FND-001, FND-011
    begin transaction
    insert stable binding with immutable id
    insert generation 1 desired facts
    commit

replace_remote_instance_in_tx(tx, binding_id, remote_id, readback): # FND-001,FND-011
    require tx active; lock binding
    require readback proves owner/model for remote_id
    current = current instance if any
    if current exists and current.remote_id == remote_id and current.readback identity matches:
        return {changed:false, old:current, current:current}     # replay/adopt is idempotent
    if current exists: close current instance at now
    next_generation = 1 if current absent else current.generation + 1
    insert new instance(remote_id, generation=next_generation, readback_hash)
    CAS current_instance_id from expected current/null to new instance
    return {changed:true, old:current, current:new instance}

replace_remote_instance(binding_id, remote_id, readback, followup_command): # FND-001,FND-011
    begin transaction
    result = replace_remote_instance_in_tx(tx, binding_id, remote_id, readback)
    if followup_command and (result.changed or required follow-up Outbox absent):
        insert deduped follow-up Outbox in SAME tx
    commit
    publish optional wake-up notification after commit
    return result.current

advance_current_generation(resource, expected, next):     # FND-011
    require next > expected
    update current pointer where generation = expected
    require affected_rows == 1
    never update rows belonging to expected generation

resolve_generation_change_replay(repository, owner, resource, kind, change_identity, payload_hash): # FND-011
    existing = repository.find_unique(owner, resource, kind, change_identity)
    if absent: return NOT_COMMITTED
    require existing.payload_hash == payload_hash
    current = repository.current(resource, kind)
    if current.id == existing.id: return ADOPT_EXISTING(existing)
    if current.generation > existing.generation: return SUPERSEDED(existing, current)
    otherwise fail invariant                                  # never move current backward
```

## 3. Ban membership generation

```text
advance_ban_membership_generation_in_tx(tx, credential, reason): # FND-014
    require caller == S05
    require tx is active and owns credential lock
    require reason in {
        PROVIDER_TOKEN_INSTANCE_ADDED,
        PROVIDER_TOKEN_INSTANCE_REMOVED,
        OWNED_ROUTING_LISTING_ADDED,
        OWNED_ROUTING_LISTING_REMOTE_DELETE_CONFIRMED
    }
    update a6_credentials
        set ban_membership_generation = ban_membership_generation + 1
        where id = credential and ban_membership_generation >= 0
    require affected_rows == 1
    append immutable audit in the SAME tx(credential, reason, new_generation, actor=tx.audit_actor)
    return new_generation              # caller commits membership + bump + audit together

# 明确禁止以下 reason 调用 advance_ban_membership_generation:
# PRICE_CHANGED / ROUTE_CHANGED / MAX_RATIO_CHANGED / PIN_CHANGED
# LISTING_LISTED / LISTING_UNLISTED / COOLING_CHANGED / LOCAL_RETIREMENT
# EXCLUSION_GATE readback 也不改变 membership generation。

read_ban_membership_generation(credential):                # FND-014
    return credential.ban_membership_generation (read-only)
```

## 4. Secret generation

```text
store_secret(credential_id, plaintext, actor):             # FND-004, FND-005
    require actor has credential_write scope
    data_key = CSPRNG()
    ciphertext, nonce = AEAD_ENCRYPT(data_key, plaintext, aad=credential_id)
    wrapped_key = KMS_WRAP(data_key, configured_kek_id)
    fingerprint = HMAC_SHA256(fingerprint_key, plaintext)
    last4 = last_four(plaintext)
    zeroize(data_key, plaintext_buffer)
    insert immutable secret_generation(ciphertext, nonce, wrapped_key,
        algorithm_version, fingerprint, last4, actor)
    return secret_generation_id

activate_secret(credential_id, expected_generation, new_generation): # FND-004,FND-005
    begin transaction
    lock credential
    require remote_identity_check(new_generation) == credential.a6_user_id
    CAS active_secret_generation from expected to new
    commit

with_secret(reference, scope):                              # FND-005
    authorize(scope)
    plaintext = decrypt(reference)
    try: yield SecretValue(plaintext, non_serializable=true) as scoped context value
    finally: zeroize(plaintext)
```

## 5. Redaction

```text
redact(event):                                             # FND-005
    for header in [Authorization, Cookie, Set-Cookie, X-API-Key]: replace value
    recursively replace values for token/key/session/password/secret fields
    replace known active secret substrings before sink write
    preserve fingerprint, last4, operation_id, request_id
    return event

install_redaction_at_all_sinks():                       # FND-005
    configure application logger, HTTP trace, audit summary, error serializer,
              metrics labels and panic reporter to call redact
```

## 6. Corpus validation and selection

```text
ACTIVE_WORKBOOK = "resources/corpora/merchant_templates_split.xlsx"
ACTIVE_TOKEN_NAMES = "resources/corpora/token_name_corpus.json"

load_corpus_catalog(path, kind):                            # FND-006
    require path in [ACTIVE_WORKBOOK, ACTIVE_TOKEN_NAMES]
    reject archive corpus paths for active selection
    bytes = read_exact(path)
    sha = SHA256(bytes)
    if kind == MERCHANT_OR_CHANNEL:
        workbook = parse_xlsx(bytes)
        require sheets include 商户资料 and 渠道资料
        validate required columns and non-empty normalized values
    else if kind == TOKEN_NAME:
        values = parse_json_array(bytes)
        validate each is non-empty string and platform-valid
    reject duplicate semantic values that make provenance ambiguous
    return Catalog(sha, parsed_rows)

select_public_text(resource, kind, selection_generation, mode, requested_ref?, followup_command?): # FND-006..009
    begin transaction
    lock resource and catalog allocation scope
    existing = find selection(resource, kind, selection_generation)
    if existing:
        if followup_command and required follow-up Outbox absent: insert deduped follow-up Outbox in SAME tx
        commit; return existing                               # retry reuse + lost-notification repair
    catalog = load current versioned catalog
    candidates = valid unallocated entries for remote uniqueness scope
    if candidates empty:
        set TEXT_CORPUS_INVALID owner fact
        insert corresponding owner/reprojection Outbox in SAME tx
        commit; return BLOCKED
    if requested_ref: require requested_ref in candidates; chosen = requested_ref
    else: chosen = cryptographic_random(candidates)
    projected = project_platform_length(chosen.channel_name) only if nickname
    listing_display_name = ""
    insert selection(catalog_sha, sheet, row_or_index, exact values,
                     selection_generation, resource, chosen)
    if followup_command: insert deduped owning-resource reconcile Outbox in SAME tx
    commit
    return chosen

handle_remote_name_conflict(selection_id, followup_command): # FND-007
    begin transaction
    lock selection/resource allocation scope
    mark selection conflict with exact remote evidence
    replacement = idempotently allocate next unused corpus entry under one new selection generation
    never mutate selected text or append suffix
    insert/verify deduped owning-resource follow-up Outbox(replacement generation, followup_command) in SAME tx
    commit; return replacement
```

## 7. Migration skeleton

```text
migrate_foundation():                                      # FND-002, FND-012
    preflight report duplicate active credentials, bindings, instances,
                     tokens, listings, snapshots and reconciliations
    if conflicts: abort with deterministic conflict table
    create enum/check domains and tables
    add foreign keys NOT VALID where online migration requires
    backfill stable IDs and generations without rewriting history
    validate constraints
    add partial unique indexes for active/current rows
    record schema version and source prompt version
```

## 8. 调用方合同

```text
before_any_remote_public_text_write(resource):              # FND-006..009
    selection = require_current_valid_selection(resource)
    if missing/invalid and this write requires new remote public text:
        project TEXT_CORPUS_INVALID and reject command
    if resource field is an existing Supplier immutable remote value:
        persist/retain LEGACY_REMOTE audit fact (source=LEGACY_REMOTE,
        writeable=false, activation_blocking=false)
        do not require rename/replacement; do not use it as write source
    return immutable selection
```

## 需求覆盖索引

本稿实现：`FND-001`, `FND-002`, `FND-003`, `FND-004`, `FND-005`, `FND-006`, `FND-007`, `FND-008`, `FND-009`, `FND-010`, `FND-011`, `FND-012`, `FND-013`, `FND-014`。
