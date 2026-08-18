# S14 Outbox 与 Reconciler：实现伪代码

[自然语言](logic.md) | [测试](tests.md) | [总纲](../../README.md)

## 1. Outbox 写入

```text
commit_desired_change(resource, expected_generation, next_generation, kind, payload): # ASY-001,002,008
    require next_generation > expected_generation
    begin transaction
    lock resource current desired pointer
    CAS business desired facts from expected_generation -> next_generation
    require affected_rows == 1
    mark older pending operations for same resource/kind SUPERSEDED
    insert outbox(resource, next_generation, kind,
        identity=stable_hash(resource, next_generation, kind, canonical_payload_hash(payload)))
        on unique conflict verify identical payload hash
    commit
    publish notification only after commit; poller remains fallback

on_eligibility_count_change(tx, listing, before, after):      # ASY-008
    require tx is the transaction committing the eligibility generation
    if before > 0 and after > 0: update projection/metrics only
    if (before == 0) != (after == 0): insert deduped owner Outbox in SAME tx
    after commit notification may wake worker but is not correctness-critical
```

## 2. Claim 与锁

```text
claim_next(worker):                                            # ASY-003,004
    select ready outbox row FOR UPDATE SKIP LOCKED
    acquire advisory/row locks in order incident, credential,
            token_or_channel, listing
    reject if any lower-level lock already held before upper
    set lease_owner, lease_until=now+30s, claim_count++       # claim is not a business retry

renew_lease(op):                                          # ASY-004
    every configurable 10s CAS owner and lease_not_expired -> now+30s
    on failure mark local ownership lost and forbid next remote mutation
```

## 3. Handler loop

```text
run_operation(op):                                             # ASY-005..007
    journal = load append-only phases
    require op generation still current; else supersede
    remote = owning_handler.read_remote_fact_before_resume(journal)
    phase = owning_handler.derive_resume_phase(journal, remote)
    while not complete:
        require lease valid and generation current
        outcome = owning_handler.execute_one_phase(op, phase)
        append journal phase outcome, redacted hashes, readback completeness
        if outcome CONFIRMED: phase = next
        if outcome UNKNOWN:
            resolution = S01 readback classify
            phase = adopt/replay/replan as owner specifies
        if outcome RETRYABLE:
            retry_round += 1
            if retry_round >= 8:
                mark DEAD_LETTER_VISIBLE; preserve conservative admission; release
            else schedule exponential_backoff(retry_round, max=30m); release
        if outcome in [REMOTE_REJECTED, NON_RETRYABLE]:
            mark DEAD_LETTER_VISIBLE(non_retryable=true, exact evidence)
            preserve conservative admission; release            # no blind eight-round replay
```

## 4. 人工重放

```text
replay_dead_letter(op_id, actor):                              # ASY-007
    require actor authorized for operation department
    op = load identity and desired generation
    require desired generation still current
    reset retry_round=0 and retry scheduling but retain operation identity, phase journal and history
    enqueue; do not clear business hold/paused state directly
```

## 5. Shared poll scheduler

```text
poll_credential(credential, dataset):                          # ASY-009..011
    acquire per-credential semaphore(2) and global semaphore(10), token bucket RPS
    result = S01 complete read for dataset
    if 429:
        reschedule only dataset using retry-after; retain last confirmed facts
        emit metric, no down/cooling event
    else if result failed or result.complete == false:
        persist poll-attempt evidence/metrics only; retain current confirmed snapshot;
        do NOT fan-out absence/clear/down events
    else:
        begin transaction
        persist new confirmed snapshot(completeness=true, hash, watermark, fetched_at)
        insert deduped affected-resource Outbox rows in same transaction
        commit
        publish wake-up notification

schedules = {
  marketplace_topn_fx:60s, platform_models:5m,
  circuit_active:15s, supplier_incremental:60s,
  supplier_full:5m, class3_billing:15m,
  drift_sweep:5m, pricing_coalesce:30s
}
Class2 account refresh uses the existing native refresh scheduler.
platform model change, safety-bound correction, cost increase and eligible-to-zero
enqueue immediate priority work instead of waiting for the periodic interval.
```

## 6. Event identity

```text
dedupe_key(event):                                             # ASY-012,013
    switch event.kind:
      attempt -> attempt_id
      terminal -> request_id + listing_binding_id
      circuit -> cooling_epoch
      route_repair -> account_id + route_generation + failure_fingerprint
      pricing -> listing/model generation + desired_hash
      default -> resource + generation + kind

submit_terminal(event):                                   # ASY-013
    require caller role == request aggregator
    duplicate stream/callback append evidence to existing event only
```

## 7. Metrics

```text
emit control_outbox_depth/age by kind
emit operation_phase/retries/dead_letters/lease_losses
emit credential_poll_age/completeness/429/generation_lag
emit remote_mutation_outcome/readback_mismatch
emit ban_init_pending/ban_init_failed/ban_relation_missing/self_route_detected/self_route_repair
pass every label/log through redactor; never label by full credential material
```

## 需求覆盖索引

本稿实现：`ASY-001`, `ASY-002`, `ASY-003`, `ASY-004`, `ASY-005`, `ASY-006`, `ASY-007`, `ASY-008`, `ASY-009`, `ASY-010`, `ASY-011`, `ASY-012`, `ASY-013`, `ASY-014`。
