# S13 Listing 状态与恢复：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Hold 与 desired

```text
ensure_hold(listing, type, owner, source_key, generation):     # REC-001,002
    begin transaction
    upsert unique owner/source generation fact; append audit
    if hold changed: insert desired/admission reprojection Outbox in SAME tx
    commit

clear_hold(listing, type, owner, source_key, generation, witness): # REC-002
    require caller == recorded owner
    require source key/generation current and witness matches exit contract
    begin transaction
    close hold fact; never delete history
    insert desired/admission reprojection Outbox in SAME tx
    commit

project_desired(listing):                                     # REC-003
    desired = LISTED if parent_binding ACTIVE and parent_channel ACTIVE
                        and active_holds.count == 0 else UNLISTED
    transactionally write desired generation + S14 Outbox if changed
```

## 2. Observer 与 admission

```text
observe_listing(listing):                                     # REC-004,006
    market = S01 complete/unique marketplace match(listing, channel, model)
    circuit = S01 circuit snapshot
    begin transaction
    if ordinary read transport failure or market/circuit incomplete:
        mark observed stale; retain last confirmed value
    else if mutation outcome unknown or complete facts contradictory/non-unique:
        write observed=UNKNOWN with evidence
    else write derived LISTED/UNLISTED/COOLING generation
    insert deduped planner/admission refresh Outbox in SAME tx
    commit

project_admission(listing):                                   # REC-004,014
    OPEN iff desired==LISTED and observed==LISTED and
        parent_channel_admission==OPEN and parent_channel.maintenance_state==NONE and
        market snapshot current+complete and circuit snapshot current+complete and
        market exact visible+available and circuit no blocking state and
        binding/key/price/eligibility/all witness generations current
        and maintenance_visibility_override == NONE
    otherwise CLOSED

persist_ban_pre_state_once(listing, op):                       # REC-015
    begin transaction
    existing = immutable maintenance_pre_state(op.id, listing.id)
    if existing: commit; return existing
    pre = exact Supplier Listing readback BEFORE first temporary mutation
    persist immutable pre under unique(op.id, listing.id)
    commit; return pre

temporary_list_for_ban_init(listing, op):                     # REC-015
    require op.caller == S05 BAN_INIT operation
    pre = persist_ban_pre_state_once(listing, op)
    begin transaction
    bind maintenance_visibility_override=TEMP_VISIBLE_FOR_BAN_INIT to op.id
    set admission CLOSED; desired unchanged
    append maintenance phase evidence in SAME tx
    commit                                      # override/pre-state is durable BEFORE remote mutation; parent S05 op already owns progress
    resolution = S01.execute_mutation(op child, exact Listing pre-read,
                                     POST listing/resubmit, exact Supplier Listing post-read)
    if resolution == CONFIRMED_ADOPT: return CONFIRMED
    keep override/pre-state and admission CLOSED; return PENDING
    # IMPORTANT: do not poll Marketplace visibility here

restore_pre_ban_state(listing, op):                           # REC-015
    require maintenance_visibility_override belongs to op or persisted pre-state unresolved
    pre = require immutable maintenance_pre_state(op.id, listing.id)
    resolution = S01 execute_mutation/readback restoring exact pre.remote_state only if current differs
    if resolution == CONFIRMED_ADOPT or exact current readback already equals pre.remote_state:
        begin transaction
        clear matching maintenance_visibility_override
        keep desired untouched; insert admission/planner refresh Outbox in SAME tx
        commit; return CONFIRMED
    remain CLOSED and keep override/pre-state until same operation resolves; return PENDING

ban_override_active(listing, op):                             # REC-015
    return maintenance_visibility_override belongs to op and is unresolved
```

## 3. Planner

```text
choose_plan(listing):                                         # REC-005,007,008,015
    if parent_channel.maintenance_state != NONE: return NONE_FOR_ORDINARY_PLANNER
        # Channel maintenance owner controls remote state; no child relist/reset/takedown
    if maintenance_visibility_override != NONE: return NONE_FOR_ORDINARY_PLANNER
        # only the owning S05 maintenance operation may continue/restore it
    if current operation exists and current generation: return CONTINUE_CURRENT
    if observed==UNKNOWN: return VERIFY_UNKNOWN
    if desired==UNLISTED and observed!=UNLISTED: return ALIGN_UNLISTED
    if desired==UNLISTED: return NONE
    if observed==COOLING: return COOLING_RESET
    if observed==UNLISTED: return DIRECT_RELIST
    if observed==LISTED and readiness current: return NONE
    return VERIFY_UNKNOWN
```

## 4. 普通恢复

```text
execute_listing_mutation_phase(op, action, target):           # REC-006,008,010,012,014
    resolution = S01.execute_mutation(op child, exact Supplier Listing pre-read,
                                     typed action, exact Supplier Listing post-read)
    if resolution == CONFIRMED_ADOPT: append confirmed phase journal; return CONFIRMED
    if resolution == REPLAY_SAME_OPERATION: keep CLOSED; return RETRY_SAME_ID
    if resolution == REMOTE_DIVERGED: keep CLOSED; supersede/replan from observed fact; return REPLAN
    if resolution in [READBACK_INCOMPLETE, RETRY_LATER]: keep CLOSED; return PENDING
    if resolution == REMOTE_REJECTED: keep CLOSED; visible failure/manual policy; return FAILED

direct_relist(op):                                            # REC-008,010,014
    require desired/current generations still match op and parent maintenance NONE
    require execute_listing_mutation_phase(op, RESUBMIT, LISTED) == CONFIRMED
    market = poll asynchronously until exact visible+available with COMPLETE snapshot
    circuit = poll until complete and no cooling/half_open/model_excluded
    require all generations equal op desired generation
    then project admission OPEN
    on timeout remain CLOSED and retry through S14; never use Supplier fact as availability
```

## 5. Local cooldown

```text
local_cooldown_deadline(history):                              # REC-009,013
    return now + [15s,120s,10m capped][failure_streak]

release_local_cooldown(hold):                             # REC-009
    at scheduled deadline check S09 eligible, effective Key/binding,
        Sub2 DB/Redis/Relay/Scheduler readiness and native account health
    if incident member require current incident health witness
    do not issue data-plane probe
    LocalCooldown owner clears matching hold then ordinary planner runs
```

## 6. Remote cooling reset

```text
cooling_epoch(circuit):                                       # REC-011
    return hash(scope, channel, model, state, cooling_until, error_kind)

cooling_reset(op):                                            # REC-011..014
    require one operation per epoch and scope and parent maintenance NONE
    phase TAKEDOWN: require execute_listing_mutation_phase(op, TAKEDOWN, UNLISTED) == CONFIRMED
    phase WAIT: schedule_at max(now+15s, cooling_until+jitter); no blocking sleep
    phase RESUBMIT: even if observed became UNLISTED, continue same op;
                    require execute_listing_mutation_phase(op, RESUBMIT, LISTED) == CONFIRMED
    phase VERIFY: require COMPLETE marketplace/circuit + generation witness then OPEN
    if recurrence within 5m use 120s; subsequent cap 10m and alert
```

Channel-scope operations create one parent journal whose children only record per-model evidence.

## 需求覆盖索引

本稿实现：`REC-001`, `REC-002`, `REC-003`, `REC-004`, `REC-005`, `REC-006`, `REC-007`, `REC-008`, `REC-009`, `REC-010`, `REC-011`, `REC-012`, `REC-013`, `REC-014`, `REC-015`。
