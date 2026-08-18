# S08 Listing 定价策略与分布：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Band 计算

```text
compute_band(model, policy, required_profiles, accounts):      # PRC-001..004
    hard_min = Decimal(policy.min default "0.025")
    hard_max = Decimal(policy.max default "4.0")
    require 0 < hard_min <= hard_max <= 4
    require required_profiles non-empty
    cap = min(Decimal("0.5") if profile_delta_capable(p) else Decimal("0")
              for p in required_profiles)
    ready_c1 = accounts where Class1 and native schedulable and capability current
              and route/cost current and current Token instance BAN_INIT READY
    if ready_c1 empty: floor = null; raw_lower = hard_min
    else:
        floor = min(a.sk*(1+a.profit)/(1+cap) for a in ready_c1)
        raw_lower = max(hard_min, floor)
    return {hard_min, hard_max,
            lower:min(raw_lower, hard_max), upper:hard_max,
            raw_lower, fixed=(min(raw_lower,hard_max)==hard_max)}
```

## 2. 桶与目标分配

```text
build_targets(model, band, listings, market, price_order):    # PRC-005..007
    managed = all non-retired controlled Listings for model sorted stable ID
    samples = [quote.public_k/Decimal("1.2") for quote in market
               if quote.callable and not quote.owned]
    samples = [clip(s, band.lower, band.upper) for s in samples]
    if band.lower == band.upper:
        targets = [band.lower] * len(managed)               # single point, no log
    else if samples empty:
        # zero sample: no new market distribution invented,
        # but MUST still pass through Merchant Price Order below
        targets = []
        for listing in managed:
            if listing existing:
                targets.append(clip(listing.current_ck,
                                    band.hard_min, band.hard_max))
            else:
                targets.append(band.lower)
    else:
        edges = eight_equal_intervals_in_log_space(band.lower, band.upper)
        frequencies = count samples per bucket
        allocation_counts = largest_remainder(frequencies,
                                              total=len(managed),
                                              deterministic_bucket_tie_order)
        targets = []
        for bucket,count in allocation_counts:
            targets += actual_quantile_points(samples in bucket, count,
                                              bounded by bucket and band)

    # 所有分支统一从这里继续，zero-sample 也不得绕过 Merchant Price Order：
    ordered_targets = sort(targets)                        # global ascending slots
    merchant_groups = group managed by merchant
                      ordered by price_order.merchant_price_rank
    slices = allocate contiguous ordered_targets by listing count per merchant
    result = {}
    for merchant in merchant_groups:
        # 1-D L1 optimal matching: sorted current prices zip sorted target prices.
        current_sorted = sort merchant.listings by (current_ck, stable_binding_id)
        target_sorted = sort slices[merchant.id] ascending
        result += zip(current_sorted, target_sorted)             # O(n log n), deterministic
    require every pair rank_i < rank_j:
        max_ck(result, rank_i) <= min_ck(result, rank_j)
    return result
```

## 3. Plan 与 apply

```text
plan_price_change(listing, desired_ck, generation, band, official_generation, fx_generation): # PRC-008..012
    require band.hard_min <= desired_ck <= band.hard_max
    coalesce by model+generation; respect batch<=10 and last write>=5m
    last = listing.last_confirmed_ck
    begin transaction
    persist operation snapshot {
        desired_ck, old_generation=generation,
        last_confirmed_ck=last,
        hard_min=band.hard_min, hard_max=band.hard_max,
        official_generation, fx_generation,
        pricing_plan_generation=generation,
        merchant_price_order_generation=current price_order generation,
        market_snapshot_hash=current complete market snapshot hash
    }
    if desired_ck < last:
        write conservative S08 economic/desired-price generation min(last, desired_ck)
        set phase=WAIT_OLD_INFLIGHT and close/fence new optimistic dispatch as needed
        insert drain/reconcile Outbox in SAME tx
    else if desired_ck > last:
        set phase=WRITE_REMOTE_FIRST; insert price-apply Outbox in SAME tx
    else set phase=NOOP
    commit

reconcile_price_change(op):                                  # PRC-008..012
    if op.phase == WAIT_OLD_INFLIGHT:
        if inflight(op.old_generation)>0:
            if timeout and op.last_confirmed_ck<=op.hard_max:
                transactionally cancel ordinary change + restore op.last_confirmed_ck generation + insert reprojection Outbox
            if op.last_confirmed_ck>op.hard_max:
                keep admission CLOSED; return RETRYABLE through S14
                # retry rounds may reach visible DLQ, but safety state never restores the >hard_max generation;
                # owner/manual replay continues the same correction identity after review.
            return
        op.phase = WRITE_REMOTE
    require op pricing_plan/merchant_price_order/official/fx/hard-bound source generations still current; else supersede/replan
    official = S07.load_model_price_generation(op.official_generation)
    fx = S07.load_fx_generation(op.fx_generation)
    desired_vector = S07.target_listing_vector(
        official.integer_vector, op.desired_ck, fx.value)
    result = S07.apply_price_vector(op, desired_vector)
    if result == CONFIRMED_ADOPT:
        consume S07 confirmed generation; mark S08 operation complete; do NOT publish a second confirmed price fact
    else if result == REPLAY_SAME_OPERATION:
        pause subsequent pricing; replay same operation after exact-old readback
    else if result == REMOTE_DIVERGED:
        pause subsequent pricing; supersede and replan from remote fact
    else if result in [READBACK_INCOMPLETE, RETRY_LATER]:
        pause subsequent pricing; retry readback under same op
    else if result == REMOTE_REJECTED:
        pause subsequent pricing; classify/report; do not blind replay
```

## 4. FX/事实门

```text
precondition_for_new_plan():
    require S07 current official model-price generation
    require S07 FX generation allow_new_price_write
    require market snapshot COMPLETE; if complete sample_count==0 then and only then apply PRC-007 zero-sample rule
    require current hard-bound generation and current Merchant Price Order generation

apply_pricing_policy_command(op):                            # PRC-001,008, ADM-005
    require caller via S14 and op.expected_generation == current hard-bound policy generation(op.model)
    if op.kind == SET_HARD_BOUNDS:
        hard_min = Decimal(op.hard_min); hard_max = Decimal(op.hard_max)
        validate 0 < hard_min <= hard_max <= 4
        begin transaction
        existing = S08 hard-bound generation by unique change_identity=op.identity
        if existing:
            verify identical policy payload; ensure required replan/correction Outbox rows exist; commit; return ADOPT_EXISTING
        require current hard-bound generation == op.expected_generation
        write new S08 hard-bound policy generation(change_identity=op.identity) + audit actor
        for affected Listing whose current S07 confirmed_ck is outside [hard_min,hard_max]:
            write current S08 desired-price correction generation(target_ck=clip(confirmed_ck, hard_min, hard_max), safety_fence=CLOSED_UNTIL_CONFIRMED)
            insert S13 admission-reprojection + price-correction Outbox rows in SAME tx
        insert ordinary affected pricing-replan Outbox rows in SAME tx
        commit
    else reject unsupported kind

apply_merchant_price_order_command(op):                       # PRC-006, ADM-013（S08 owning handler）
    require op.kind == SET_MERCHANT_PRICE_ORDER and caller via S14
    require op.expected_generation == current merchant_price_order_generation
    validate op.proposal has one unique active rank per active merchant and active rank 全站唯一
    begin transaction
    existing = merchant_price_order generation by unique change_identity=op.identity
    if existing:
        verify identical rank payload; ensure its pricing-replan Outbox exists; commit; return ADOPT_EXISTING
    require current merchant_price_order_generation == op.expected_generation
    write new a6_merchant_price_orders generation(change_identity=op.identity) and audit actor
    insert exactly one S08 pricing-replan Outbox row for the new order generation
    commit
    publish optional wake-up notification after commit
```

## 需求覆盖索引

本稿实现：`PRC-001`, `PRC-002`, `PRC-003`, `PRC-004`, `PRC-005`, `PRC-006`, `PRC-007`, `PRC-008`, `PRC-009`, `PRC-010`, `PRC-011`, `PRC-012`。
