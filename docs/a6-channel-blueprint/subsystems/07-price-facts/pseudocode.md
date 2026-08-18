# S07 价格事实、ck 与 FX：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Price vector

```text
DIMENSIONS = [input, output, cache_read, cache_write]
COMMISSION = Decimal("0.2")

choose_canonical(official):                                  # PRF-003
    return first d in DIMENSIONS where official[d] > 0
    or CONFIG_INVALID if all zero

target_listing_vector(official, target_ck, fx):              # PRF-002..005
    require target_ck>0 and fx>0
    vector = {}
    for d in DIMENSIONS:
        if official[d] > 0:
            vector[d] = ceil_integer(Decimal(official[d]) * target_ck / fx)
        else:
            vector[d] = protocol_zero_value
    return vector

confirm_listing_vector(official, remote_vector, fx, target_ck, expected_vector): # PRF-004,005,012
    canonical = choose_canonical(official)
    ck_by_dimension = {}
    envelope = {}
    for d where official[d]>0:
        require remote_vector[d] is nonnegative integer
        require remote_vector[d] == expected_vector[d]          # remote must equal our exact ceil target
        ck_d = fx * Decimal(remote_vector[d]) / Decimal(official[d])
        one_micros_ck = fx / Decimal(official[d])
        require target_ck <= ck_d < target_ck + one_micros_ck   # only the local ceil error envelope is allowed
        ck_by_dimension[d] = ck_d
        envelope[d] = one_micros_ck
    return generation(canonical_ck=ck_by_dimension[canonical],
                      confirmed_ck=min(ck_by_dimension.values),
                      ck_by_dimension, quantization_envelope=envelope,
                      vector=remote_vector, expected_vector, target_ck,
                      official=official, fx=fx)

price_vector_quantization_envelope(snapshot):                 # PRF-005,012
    return max per-dimension allowed earning/micros effect derived from snapshot.quantization_envelope
```

## 2. 版本化价格事实与 Markup/config audit

```text
commit_price_fact_generation(scope, kind, payload, source_evidence, followup): # PRF-006,007,010..012
    begin transaction
    lock scope/kind current pointer
    if current generation has identical source hash + payload hash: return current (no duplicate generation)
    insert immutable generation(payload, source timestamp/hash, rounding policy, canonical dimension)
    advance current pointer with CAS
    if followup: insert owning recompute/audit Outbox rows in SAME tx
    commit
    return new generation

ingest_official_model_prices(snapshot):                       # PRF-002,003,012
    require snapshot COMPLETE and every official micros vector is nonnegative integer
    for model in snapshot.models:
        canonical = choose_canonical(model.official_vector)
        commit_price_fact_generation(model, MODEL_PRICE,
            {integer_vector=model.official_vector, canonical}, snapshot.evidence,
            followup=PRICE_CONFIG_AND_PRICING_RECOMPUTE)

audit_output_config(credential, channels, listings, model_generation, fx_generation): # PRF-006,007,012
    preview = S01.batch_markup_preview([c.remote_channel_id for c in channels], credential)
    if preview incomplete or channel_id mapping missing/duplicate:
        transactionally ensure OUTPUT_CONFIG_INVALID + config-repair Outbox; return INVALID
    official = load immutable MODEL_PRICE generation(model_generation)
    fx = load immutable FX generation(fx_generation)
    for listing in listings:
        errors = []
        item = preview[listing.parent_remote_channel_id]
        if item.percent != Decimal("20") or item.source invalid: errors += MARKUP_DRIFT
        if listing.charge_type != per_token: errors += CHARGE_TYPE_DRIFT
        if listing.billing/model ref != model_generation identity: errors += MODEL_PRICE_REF_DRIFT
        expected_vector = target_listing_vector(official.integer_vector, listing.target_ck, fx.value)
        if confirm_listing_vector(official.integer_vector, listing.remote_vector, fx.value,
                                  listing.target_ck, expected_vector) fails: errors += PRICE_VECTOR_DRIFT
        begin transaction
        append immutable config observation(errors, preview/model/fx source hashes)
        if errors:
            ensure S07-owned OUTPUT_CONFIG_INVALID + owning repair Outbox in SAME tx
        else:
            publish/retain confirmed Listing price fact and clear only matching S07 config-invalid source + downstream recompute Outbox in SAME tx
        commit
```

## 3. FX resolver

```text
resolve_fx(a6_snapshot, now):                                # PRF-010,011,012
    if a6_snapshot COMPLETE and has positive CNY/USD ratios from the same snapshot:
        value = Decimal(a6_snapshot.cny_ratio) / Decimal(a6_snapshot.usd_ratio)
        return commit_price_fact_generation(FX_USD_CNY, FX,
            {value, source=A6, allow_new_price_write=true}, a6_snapshot.evidence,
            followup=PRICING_RECOMPUTE)
    cached = fresh persisted Frankfurter quote with age<=30m and source_date_age<=96h
    if cached:
        return commit_price_fact_generation(FX_USD_CNY, FX,
            {value=cached.value, source=FRANKFURTER, allow_new_price_write=true}, cached.evidence,
            followup=PRICING_RECOMPUTE)
    result = GET https://api.frankfurter.app/latest?from=USD&to=CNY timeout=3s
    if valid Decimal(result.rates.CNY) and source_date_age<=96h:
        persist fallback quote/cache metadata; refresh active fallback every 15m
        return commit_price_fact_generation(FX_USD_CNY, FX,
            {value=Decimal(result.rates.CNY), source=FRANKFURTER, allow_new_price_write=true}, result.evidence,
            followup=PRICING_RECOMPUTE)
    last = current confirmed FX generation
    require last exists
    return immutable view(last, source=LAST_CONFIRMED, read_only=true, allow_new_price_write=false)

load_model_price_generation(id):                              # PRF-012
    return immutable MODEL_PRICE generation by id

load_fx_generation(id):                                       # PRF-012
    return immutable FX generation by id
```

## 4. ToA6API resolver

```text
resolve_base_actual_cost(request_usage, model, generation, group): # PRF-008
    if group != exact ToA6API: return existing_native_price_resolver(request_usage, model, generation, group)
    require generation current at dispatch or frozen on attempt
    vector = A6 official USD model-price vector from generation
    return existing_usage_cost_formula(request_usage, vector) using Decimal
```

## 5. Remote price write outcome

```text
apply_price_vector(op, desired_vector):                       # PRF-007,009,012
    official = load_model_price_generation(op.official_generation)
    fx = load_fx_generation(op.fx_generation)
    resolution = S01.execute_mutation(op, exact listing pre-read, PUT listing desired_vector, exact listing post-read)
    if resolution == CONFIRMED_ADOPT:
        remote_vector = resolution.post_read.price_vector
        confirmed = confirm_listing_vector(official.integer_vector, remote_vector, fx.value, op.desired_ck, desired_vector)
        commit_price_fact_generation(op.listing_instance_id, LISTING_PRICE, confirmed,
            resolution.post_read.evidence, followup=ELIGIBILITY_AND_REVENUE_RECOMPUTE)
    else:
        retain last_confirmed_ck/model generation; never publish desired as observed
    return resolution                                         # preserve old/third/reject/incomplete to S08
```

## 需求覆盖索引

本稿实现：`PRF-001`, `PRF-002`, `PRF-003`, `PRF-004`, `PRF-005`, `PRF-006`, `PRF-007`, `PRF-008`, `PRF-009`, `PRF-010`, `PRF-011`, `PRF-012`。
