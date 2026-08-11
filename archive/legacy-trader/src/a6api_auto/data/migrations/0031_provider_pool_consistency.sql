-- Bind Provider pool rows to the exact model/generation/token/account tuple.
-- The initial pool schema had independent scalar FKs.  Those allowed a valid
-- id from one model or generation to be paired with columns from another.
-- This migration adds composite keys and future-write checks without changing
-- historical generation semantics.

-- Composite references need a matching unique key on the parent tables.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_cost_generation_id_model_generation_uq'
          AND conrelid = 'a6_trade.provider_cost_generations'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_cost_generations
            ADD CONSTRAINT provider_cost_generation_id_model_generation_uq
            UNIQUE (id, model, cost_generation);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_generation_id_model_generation_uq'
          AND conrelid = 'a6_trade.provider_pool_generations'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_generations
            ADD CONSTRAINT provider_pool_generation_id_model_generation_uq
            UNIQUE (id, model, generation);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'a6_pin_binding_id_model_token_channel_uq'
          AND conrelid = 'a6_trade.a6_pin_bindings'::regclass
    ) THEN
        ALTER TABLE a6_trade.a6_pin_bindings
            ADD CONSTRAINT a6_pin_binding_id_model_token_channel_uq
            UNIQUE (id, model, a6_token_id, channel_id);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'enodot_binding_id_token_account_uq'
          AND conrelid = 'a6_trade.enodot_account_bindings'::regclass
    ) THEN
        ALTER TABLE a6_trade.enodot_account_bindings
            ADD CONSTRAINT enodot_binding_id_token_account_uq
            UNIQUE (id, a6_token_id, enodot_account_id);
    END IF;
END
$$;

-- A pool generation must point at the cost row carrying the same model and
-- cost generation, not merely at an arbitrary cost row with the same id.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_generation_cost_identity_fk'
          AND conrelid = 'a6_trade.provider_pool_generations'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_generations
            ADD CONSTRAINT provider_pool_generation_cost_identity_fk
            FOREIGN KEY (cost_generation_id, model, cost_generation)
            REFERENCES a6_trade.provider_cost_generations(id, model, cost_generation)
            NOT VALID;
    END IF;
END
$$;

-- Every slot belongs to the exact routing and cost generations named in its
-- row.  NOT VALID keeps an already-populated history upgradeable; new writes
-- are checked immediately.  The deployment verifier can VALIDATE after an
-- observed-history cleanup window.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_routing_identity_fk'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_routing_identity_fk
            FOREIGN KEY (generation_id, model, routing_generation)
            REFERENCES a6_trade.provider_pool_generations(id, model, generation)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_cost_identity_fk'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_cost_identity_fk
            FOREIGN KEY (cost_generation_id, model, cost_generation)
            REFERENCES a6_trade.provider_cost_generations(id, model, cost_generation)
            NOT VALID;
    END IF;
END
$$;

-- Inventory and pin binding rows must agree on token/account identity.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_inventory_binding_identity_fk'
          AND conrelid = 'a6_trade.provider_inventory'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_inventory
            ADD CONSTRAINT provider_inventory_binding_identity_fk
            FOREIGN KEY (enodot_binding_id, a6_token_id, enodot_account_id)
            REFERENCES a6_trade.enodot_account_bindings(id, a6_token_id, enodot_account_id)
            NOT VALID;
    END IF;
END
$$;

-- READY slots carry the exact Enodot binding identity, not an independently
-- valid account and token assembled from different bindings.
UPDATE a6_trade.provider_pool_slots slot
SET enodot_binding_id = binding.id
FROM a6_trade.enodot_account_bindings binding
WHERE slot.enodot_binding_id IS NULL
  AND slot.a6_token_id IS NOT NULL
  AND slot.enodot_account_id IS NOT NULL
  AND slot.a6_token_id = binding.a6_token_id
  AND slot.enodot_account_id = binding.enodot_account_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_binding_identity_fk'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_binding_identity_fk
            FOREIGN KEY (enodot_binding_id, a6_token_id, enodot_account_id)
            REFERENCES a6_trade.enodot_account_bindings(
                id, a6_token_id, enodot_account_id
            )
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_ready_binding_required'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_ready_binding_required
            CHECK (slot_state <> 'READY' OR enodot_binding_id IS NOT NULL)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_vacant_bindings_empty'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_vacant_bindings_empty
            CHECK (
                slot_state <> 'VACANT'
                OR (enodot_binding_id IS NULL AND confirmed_pin_binding_id IS NULL)
            )
            NOT VALID;
    END IF;
END
$$;

-- On a clean row, a confirmed pin must be for the same model/token/channel.
-- Backfill the binding id where the current binding is unambiguous, then keep
-- the invariant enforced for future READY rows.
UPDATE a6_trade.provider_pool_slots slot
SET confirmed_pin_binding_id = binding.id
FROM a6_trade.a6_pin_bindings binding
WHERE slot.confirmed_pin_binding_id IS NULL
  AND slot.pin_confirmed
  AND slot.a6_token_id IS NOT NULL
  AND slot.model = binding.model
  AND slot.a6_token_id = binding.a6_token_id
  AND slot.desired_channel_id = binding.channel_id
  AND binding.valid_to IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_pin_identity_fk'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_pin_identity_fk
            FOREIGN KEY (
                confirmed_pin_binding_id, model, a6_token_id, desired_channel_id
            )
            REFERENCES a6_trade.a6_pin_bindings(id, model, a6_token_id, channel_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'provider_pool_slot_ready_pin_required'
          AND conrelid = 'a6_trade.provider_pool_slots'::regclass
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            ADD CONSTRAINT provider_pool_slot_ready_pin_required
            CHECK (slot_state <> 'READY' OR confirmed_pin_binding_id IS NOT NULL)
            NOT VALID;
    END IF;
END
$$;

-- A READY account/token can occupy at most one live slot for a model.  VACANT
-- rows remain exempt through the partial predicates.
CREATE UNIQUE INDEX IF NOT EXISTS provider_pool_current_ready_account_idx
    ON a6_trade.provider_pool_slots (model, enodot_account_id)
    WHERE valid_to IS NULL AND slot_state = 'READY' AND enodot_account_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS provider_pool_current_ready_token_idx
    ON a6_trade.provider_pool_slots (model, a6_token_id)
    WHERE valid_to IS NULL AND slot_state = 'READY' AND a6_token_id IS NOT NULL;

-- Validate clean installations immediately.  Existing deployments can run
-- the same statements after correcting rows reported by the query below.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_generations generation
        JOIN a6_trade.provider_cost_generations cost
          ON cost.id = generation.cost_generation_id
        WHERE generation.cost_generation_id IS NOT NULL
          AND (generation.model, generation.cost_generation)
              IS DISTINCT FROM (cost.model, cost.cost_generation)
    ) THEN
        ALTER TABLE a6_trade.provider_pool_generations
            VALIDATE CONSTRAINT provider_pool_generation_cost_identity_fk;
    END IF;
EXCEPTION WHEN undefined_object THEN
    NULL;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_slots slot
        LEFT JOIN a6_trade.enodot_account_bindings binding
          ON binding.id = slot.enodot_binding_id
         AND binding.a6_token_id = slot.a6_token_id
         AND binding.enodot_account_id = slot.enodot_account_id
        WHERE slot.enodot_binding_id IS NOT NULL
          AND binding.id IS NULL
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            VALIDATE CONSTRAINT provider_pool_slot_binding_identity_fk;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_slots
        WHERE slot_state = 'READY' AND enodot_binding_id IS NULL
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            VALIDATE CONSTRAINT provider_pool_slot_ready_binding_required;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_slots
        WHERE slot_state = 'VACANT'
          AND (enodot_binding_id IS NOT NULL OR confirmed_pin_binding_id IS NOT NULL)
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            VALIDATE CONSTRAINT provider_pool_slot_vacant_bindings_empty;
    END IF;
EXCEPTION WHEN undefined_object THEN
    NULL;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_slots slot
        JOIN a6_trade.provider_pool_generations generation
          ON generation.id = slot.generation_id
        JOIN a6_trade.provider_cost_generations cost
          ON cost.id = slot.cost_generation_id
        WHERE (slot.model, slot.routing_generation)
                  IS DISTINCT FROM (generation.model, generation.generation)
           OR (slot.model, slot.cost_generation)
                  IS DISTINCT FROM (cost.model, cost.cost_generation)
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            VALIDATE CONSTRAINT provider_pool_slot_routing_identity_fk;
        ALTER TABLE a6_trade.provider_pool_slots
            VALIDATE CONSTRAINT provider_pool_slot_cost_identity_fk;
    END IF;
EXCEPTION WHEN undefined_object THEN
    NULL;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_inventory inventory
        LEFT JOIN a6_trade.enodot_account_bindings binding
          ON binding.id = inventory.enodot_binding_id
         AND binding.a6_token_id = inventory.a6_token_id
         AND binding.enodot_account_id = inventory.enodot_account_id
        WHERE inventory.enodot_binding_id IS NOT NULL
          AND binding.id IS NULL
    ) THEN
        ALTER TABLE a6_trade.provider_inventory
            VALIDATE CONSTRAINT provider_inventory_binding_identity_fk;
    END IF;
EXCEPTION WHEN undefined_object THEN
    NULL;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_slots slot
        LEFT JOIN a6_trade.a6_pin_bindings binding
          ON binding.id = slot.confirmed_pin_binding_id
         AND binding.model = slot.model
         AND binding.a6_token_id = slot.a6_token_id
         AND binding.channel_id = slot.desired_channel_id
        WHERE slot.confirmed_pin_binding_id IS NOT NULL
          AND binding.id IS NULL
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            VALIDATE CONSTRAINT provider_pool_slot_pin_identity_fk;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM a6_trade.provider_pool_slots
        WHERE slot_state = 'READY' AND confirmed_pin_binding_id IS NULL
    ) THEN
        ALTER TABLE a6_trade.provider_pool_slots
            VALIDATE CONSTRAINT provider_pool_slot_ready_pin_required;
    END IF;
EXCEPTION WHEN undefined_object THEN
    NULL;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a6trader') THEN
        GRANT SELECT, INSERT, UPDATE ON a6_trade.provider_pool_generations,
            a6_trade.provider_pool_slots, a6_trade.provider_inventory,
            a6_trade.provider_cost_generations TO a6trader;
    END IF;
END
$$;
