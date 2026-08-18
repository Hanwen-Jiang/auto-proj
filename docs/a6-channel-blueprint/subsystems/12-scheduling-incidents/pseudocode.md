# S12 调度、错误与 Incident：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. 请求与 attempt 分类

```text
schedule_request(request, mode):                              # ERR-001..003
    if deterministic user/input/auth/policy/client error:
        return USER_SHORT_CIRCUIT before scheduler side effects
    context = S02.build_dispatch_context(request.auth, request)
    eligible = S09.current_candidates(context.listing_binding_id, context.normalized_model, context.profile)
    if eligible.count == 0:
        ensure current S09 eligibility generation has its deduped NO_ELIGIBLE owner Outbox; return NO_ELIGIBLE
    attempts = native_scheduler_with_S09_gate(eligible, mode)
    if attempts.dispatched_count == 0:
        current = S09.current_candidates(context.listing_binding_id, context.normalized_model, context.profile)
        if current.count == 0:
            ensure current S09 eligibility generation has its deduped NO_ELIGIBLE owner Outbox; return NO_ELIGIBLE
        if attempts.only_busy: return service_busy without state event
        return RETRY_SELECTION_CURRENT_GENERATION without Listing/account penalty
    return aggregate_attempts(context, request, attempts)

classify_attempt(error, context):                             # ERR-003..006
    follow errorlist decision order
    distinguish user credential vs managed Provider credential
    distinguish invalid requested model vs valid model/current route mismatch
    require attribution evidence before UPSTREAM_AUTO_POOL_EXPAND for ambiguous codes
    default unknown/unsupported evidence -> LISTING_COOLDOWN_TRIGGER

record_attempt_failure_once(attempt):                         # ERR-007
    class = classify_attempt(attempt.error, attempt.context)
    begin transaction
    upsert/retain immutable classification evidence by attempt_id; never create a second attempt state machine
    if class == A6_ACCOUNT_RECOVERABLE:
        insert deduped narrow account-owner Outbox in SAME tx
    if class == UPSTREAM_AUTO_POOL_EXPAND:
        insert deduped S06 route-owner Outbox by explicit mode in SAME tx
    if class == USER_SHORT_CIRCUIT:
        evidence only; zero account/route/Listing penalty
    commit
    return class
```

## 2. SSE request-local guard

```text
SSE_PRELUDE_MIN_MS = 500
SSE_PRELUDE_MAX_MS = 2000
SSE_PRELUDE_FRAME = ":\\n\\n"                            # one legal no-op SSE comment

init_sse_guard(request, context):                             # ERR-014
    require context.profile is SSE/stream
    require deterministic user/input/auth/policy/model checks already PASS
    # Randomness is load-spreading only, not a security primitive; use existing cheap bounded RNG.
    delay_ms = random_integer_inclusive(500, 2000)
    return {
        prelude_deadline = monotonic_now + delay_ms,
        prelude_sent = false,
        upstream_stream_committed = false
    }

on_sse_prelude_deadline(guard, downstream):                  # ERR-014
    if guard.prelude_sent or guard.upstream_stream_committed: return
    write SSE_PRELUDE_FRAME and flush once
    guard.prelude_sent = true                                # DOES NOT commit upstream response

forward_upstream_sse_event(guard, event, downstream):         # ERR-014
    # Caller must parse/classify upstream error events before invoking this forward path.
    guard.upstream_stream_committed = true                   # set before first upstream-originated write
    forward event using existing SSE adapter without rewriting event identity

handle_precommit_sse_failure(request, context, attempt, guard): # ERR-003..007,014
    class = record_attempt_failure_once(attempt)
    if guard.upstream_stream_committed:
        return EXISTING_STREAM_ERROR_PATH                    # never stitch another upstream
    if class in [A6_ACCOUNT_RECOVERABLE, UPSTREAM_AUTO_POOL_EXPAND]
       and existing errorlist/native scheduler marks this exact context candidate-failoverable:
        return TRY_NEXT_UNTRIED_CANDIDATE_THROUGH_S09_GATE   # same downstream; same guard/deadline
    if guard.prelude_sent:
        return EXISTING_STREAM_ERROR_OR_CLOSE_PATH            # headers already committed; never try a second HTTP status
    return EXISTING_ERROR_HANDLER_OR_AGGREGATE               # USER/platform/Listing/local/unknown stay native

# Integration contract for the existing stream loop:
# - initialize the guard exactly when the first upstream attempt is actually dispatched after deterministic ingress checks.
# - arm exactly one cancellable deadline per live SSE request; no ticker and no polling goroutine required.
# - if an upstream non-error event wins the race, cancel/ignore prelude timer and forward via forward_upstream_sse_event().
# - if timer wins first, emit one prelude then keep waiting on the current upstream.
# - if a failoverable error arrives before upstream commit, consume that error (do not forward it), record attempt action,
#   select the next candidate through the normal S09/native scheduler path, and keep the original guard/deadline.
# - candidate switch never resets the timer and never emits a second prelude.
# - pass the same request-local guard reference into S10 terminal context as attempt.stream; do not persist/copy it into DB generations.
# - if prelude already committed HTTP/SSE headers and no failover is allowed, reuse the existing stream error/close path; never invent a second HTTP response/error serializer.
# - client cancellation cancels timer/upstream immediately and follows USER_SHORT_CIRCUIT/no-penalty behavior.
# - process exit ends request; no durable guard/reconnect/resume.
```

## 3. 聚合

```text
aggregate_attempts(context, request, attempts):               # ERR-007,008
    class_summary = []
    for failed attempt in dispatched attempts exactly once:
        class = record_attempt_failure_once(attempt)            # own short tx; may already have run before SSE failover
        class_summary += immutable {attempt_id, class, failure_fingerprint}
    if any success:
        return S10 success path                                 # prior failed-attempt actions are already durable
    begin transaction                                           # aggregate tx starts only after per-attempt actions
    recompute_event = insert deduped S09 eligibility-recompute Outbox for listing/profile in SAME tx
    insert deduped FINALIZE_AGGREGATE Outbox(
        key=request_id+listing_binding_id+profile,
        payload={request_id, listing_binding_id, profile, source_eligibility_generation,
                 recompute_event_identity, dispatched_all_failed=true, class_summary}) in SAME tx
    commit
    return aggregate failure immediately                         # never wait for Outbox/S09 on request goroutine

finalize_aggregate(event):                                    # ERR-008
    # Reuse the immutable Outbox payload; do not create another request-terminal state machine/table.
    if S09 recompute generated from event.recompute_event_identity not yet committed:
        return RETRY_LATER                                    # async retry only; no request is waiting
    current = read current S09 structural/profile eligibility generation for event listing/profile
    if current.count == 0:
        transactionally ensure NO_ELIGIBLE only; do not add local cooldown for same terminal
        return
    if event.dispatched_all_failed and (event.class_summary has LISTING_COOLDOWN_TRIGGER
       or responsibility remains unknown):
        transactionally insert Local-Cooldown-owned command + incident-consideration Outbox once
    return
```

## 4. Fingerprint 与 Incident

实现约束：**全系统只保留一个通用 Incident 模型与一套 reconcile/recovery 流程**。Cloudflare、A6、DB、Redis、Relay、Scheduler 等差异只体现在 `scope/failure_domain/fingerprint/member/witness` 数据中；不得为每个故障域再建立独立 `*IncidentController`、独立状态机或独立恢复框架。

```text
failure_fingerprint(event):                                  # ERR-011
    canonical = normalize {
      failure_domain, canonical_error_code, endpoint_host,
      endpoint_path_class, http_method, egress_region,
      relay_generation, credential_scope
    }
    return stable_hash(canonical)          # no listing/account/channel IDs

consider_incident(event):                                    # ERR-009,010
    fp = failure_fingerprint(event)
    scope = derive exact incident scope from authoritative signal/evidence
    should_promote = authoritative global health signal
        or same request has >=2 distinct account/route identities at same endpoint
        or within 60s >=2 distinct listing IDs have fp
    if not should_promote: record listing-local evidence only; return
    begin transaction
    incident = idempotently open_or_join incident(scope, fp, generation)
    insert deduped RECONCILE_INCIDENT Outbox(incident.id, incident.generation) in SAME tx
    commit
```

## 5. Membership 与恢复

```text
reconcile_incident(incident):                                # ERR-012,013
    enumerate active Listings in exact scope
    begin transaction
    persist idempotent incident membership facts
    insert member owner Outbox rows in SAME tx
    commit
    for member:
        if local failure: LocalCooldown owner may ensure hold from its command
        if remote circuit cooling: record membership only; no local hold

recover_incident(incident, health_witness):               # ERR-013
    require witness current for incident generation
    select deterministic canary
    ask owning controller to clear its hold or S13 reset remote cooling
    require owning asynchronous operation completed with matching generation witness
    require marketplace visible/available + circuit clear witness
    for batches of <=10 remaining members with random 5..15s scheduled jitter:
        recover through S13 ordinary path
        stop immediately on first failure
    for channel scope use one parent operation and child evidence, not N channel writes
```

## 需求覆盖索引

本稿实现：`ERR-001`, `ERR-002`, `ERR-003`, `ERR-004`, `ERR-005`, `ERR-006`, `ERR-007`, `ERR-008`, `ERR-009`, `ERR-010`, `ERR-011`, `ERR-012`, `ERR-013`, `ERR-014`。
