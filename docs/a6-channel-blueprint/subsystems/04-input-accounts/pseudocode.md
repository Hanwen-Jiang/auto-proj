# S04 Class1/2/3 输入账号：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. 分类与 profit

```text
classify_account(binding):                                   # ACC-001
    require explicit class enum and provider_code when Class3
    never infer from account name, group, role or token name

effective_profit(account):                                   # ACC-011
    if account.override != null: require override >= 0; return override
    if Class3 and provider default exists: return provider default
    return {Class1:0.20, Class2:0.50, Class3:0.40}[class] as Decimal
```

## 2. Account generation commit primitive

```text
commit_account_generation(account, kind, payload, expected_generation,
                          change_identity=payload.change_identity or stable source-evidence identity,
                          followup=ELIGIBILITY_RECOMPUTE):      # ACC-004..014,FND-011
    require change_identity stable and excludes retry/time
    payload_hash = canonical_hash(payload)
    begin transaction
    lock account
    replay = S00.resolve_generation_change_replay(
        S04_GENERATIONS, S04, account.id, kind, change_identity, payload_hash)
    if replay == ADOPT_EXISTING:
        if followup and required follow-up Outbox absent: insert it in SAME tx
        commit; return replay.generation
    if replay == SUPERSEDED: commit; return replay
    current = current generation(account, kind) if any
    require (current.generation if current else NULL) == expected_generation
    insert immutable S04 generation(kind, payload, change_identity, payload_hash,
                                    evidence=payload.source_evidence if present)
    advance current pointer with CAS(expected_generation)
    if followup: insert affected S09 eligibility/account-projection Outbox rows in SAME tx
    commit
    return new generation

apply_account_economic_policy_command(op):                    # ACC-005..011
    account = authoritative account(op.account_id)
    require caller via S14 and op.expected_generation == current ECONOMIC_POLICY generation(account)
    validate op Decimal inputs and class/provider scope
    proposed = derive Class2 multiplier/cost, Class3 declared/model sk, or profit override using production evaluators
    proposed.change_identity = op.identity
    return commit_account_generation(account, ECONOMIC_POLICY, proposed, op.expected_generation)
```

## 3. Class1 cost

```text
observe_auto_pool_ratios(callable_non_owned_ratios, policy):  # ACC-002..004
    ratios = sort Decimal values ascending
    if count(ratios) < policy.min_n: return RANGE_EXHAUSTED
    N = min(policy.target_n, count(ratios))
    boundary = ratios[N-1]
    included = all ratios <= boundary             # complete equal-price band
    sk = boundary
    cv_n = population_stddev(ratios[0:N]) / mean(ratios[0:N]) if mean>0 else 0
    tail_jump = (ratios[N-1]-ratios[N-2])/ratios[N-2] if N>=2 and ratios[N-2]>0 else null
    return observation(sk, included, cv_n, tail_jump)

confirm_class1_cost(account, observation):                    # ACC-004
    require observation snapshot is complete and has stable snapshot_generation
    current = current cost generation if any
    if current absent:
        return commit_account_generation(account, COST, observation, expected_generation=NULL)
    if observation.sk > current.sk:
        return commit_account_generation(account, COST, observation, expected_generation=current.generation)
    else if observation.sk < current.sk:
        count only distinct compatible lower snapshot_generations in immutable evidence
        if distinct_count reaches 3:
            return commit_account_generation(account, COST, observation, expected_generation=current.generation)
        retain current
    else refresh immutable evidence without a new economic value/current pointer change

pin_cost(account, assignment_epoch, pin_readback):             # ACC-002
    require assignment_epoch == current S06 Pin assignment epoch(account)
    require pin_readback exact channel/model/K_CNY for assignment_epoch
    return immutable cost observation(sk=Decimal(pin_readback.K_CNY), assignment_epoch, source_evidence=pin_readback)
```

## 4. Class2 cost

```text
compute_class2_sk(account, profile, sample):                   # ACC-005..007
    multiplier = Decimal(profile.multiplier default "1")
    require multiplier > 0
    if sample.used_A > 0 and sample.utilization > 0:
        one_week_A = sample.used_A / sample.utilization
        estimated = false
    else:
        one_week_A = Decimal("100")
        estimated = true
    expected_total_A = one_week_A * multiplier
    candidate = profile.account_cost_cny / expected_total_A
    if sample fields contradictory:
        if confirmed history exists: return retain history with warning
        candidate = profile.account_cost_cny / (100 * multiplier); estimated = true
    current = current COST generation(account) if any
    commit_account_generation(account, COST, {candidate, inputs=sample, estimated},
                              expected_generation=current.generation if current else NULL)
    do not mutate native OAuth status based only on utilization >= 1
```

## 5. Class3 billing

```text
class3_sk(account, normalized_model):                         # ACC-008
    if account.model_sk_overrides contains normalized_model:
        return Decimal(account.model_sk_overrides[normalized_model])
    return Decimal(account.declared_sk)

evaluate_class3_window(account, start, end):                  # ACC-009
    samples = charged successful requests for account within [start,end) with upstream bill facts
    expected = sum(expected costs using frozen generation)
    actual = sum(upstream billed costs)
    if count(samples)<20 or expected<CNY(1): return INSUFFICIENT_WINDOW
    deviation = abs(actual-expected)/expected
    if deviation > 0.30: pause immediately(reason=SINGLE_WINDOW_GT_30)
    else if deviation > 0.15:
        increment consecutive_bad; pause when count == 3
    else:
        reset >15 streak
        if deviation > 0.05: warn
    persist immutable billing observation
    if pause threshold reached:
        current = current BILLING_GATE generation(account) if any
        commit_account_generation(account, BILLING_GATE, PAUSED(reason, observation),
                                  expected_generation=current.generation if current else NULL)

recover_class3(account):                                     # ACC-010
    require account paused for billing deviation
    resync upstream billing
    schedule bounded audit samples subject to budget
    count only qualified windows with deviation <= 0.05
    if 3 consecutive:
        current = current BILLING_GATE generation(account)
        commit_account_generation(account, BILLING_GATE, HEALTHY(resync+witnesses),
                                  expected_generation=current.generation)
    if budget insufficient: remain paused with reason AUDIT_BUDGET_INSUFFICIENT
```

## 6. Capability probes

```text
schedule_capability_probe(account, model, profile):           # ACC-012,013
    require account.class in [Class2, Class3]
    require model/profile are in the pending capability audit set for this account
    target_input_usage = deterministic bounded audit target for profile
    require 500 <= target_input_usage <= 3000
    if global concurrency/RPM/hourly cost budget exhausted:
        mark DELAYED_BY_BUDGET(account, model, profile); do not whitelist
    else enqueue bounded probe(account, model, profile, target_input_usage)

commit_capability_generation(account, observations):     # ACC-012,ACC-013,ACC-014
    if account.class == Class1:
        require observations contain current S05/S06 Token model + route + callability exact readback evidence
        payload = derive Class1 allowlist + endpoint/transport evidence from observations
    else:
        require observations prove each whitelisted normalized A6 OpenAI model by bounded probe
        payload = derive allowlist + endpoint/transport evidence from observations
    current = current CAPABILITY generation(account) if any
    return commit_account_generation(account, CAPABILITY, payload,
                                     expected_generation=current.generation if current else NULL)

commit_class1_route_facts(account, route_readback):           # ACC-002,004,014
    require route_readback is exact current Token/model/route/callability evidence from S06
    if route_readback.mode == AUTO_POOL:
        cost_observation = derive top-band sk from route_readback
    else:
        cost_observation = pin_cost(account, route_readback.assignment_epoch, route_readback.pin)
    cost_gen = confirm_class1_cost(account, cost_observation bound to route_readback.route_generation)
    cap_gen = commit_capability_generation(account, route_readback model/callability observations)
    return matching {cost_gen, cap_gen, route_generation=route_readback.route_generation}
```

## 7. Ready projection

```text
project_account_schedulability(account, model, profile):       # ACC-014
    ready = native_sub2_schedulable(account)
         and current economic generation(account) valid
         and current capability generation(account) permits model/profile
         and current billing generation(account, model) valid when Class3
         and current S05 BAN_INIT READY / S06 route generation(account, model) valid when Class1
    ignore instantaneous full-concurrency as structural fact
    begin transaction
    write versioned DB schedulability projection with composite generation/hash
    insert Redis/account-dispatch refresh Outbox for the same generation in SAME tx
    commit
    after commit fast-path replace Redis projection; if process crashes, Outbox repairs it
```

## 需求覆盖索引

本稿实现：`ACC-001`, `ACC-002`, `ACC-003`, `ACC-004`, `ACC-005`, `ACC-006`, `ACC-007`, `ACC-008`, `ACC-009`, `ACC-010`, `ACC-011`, `ACC-012`, `ACC-013`, `ACC-014`。
