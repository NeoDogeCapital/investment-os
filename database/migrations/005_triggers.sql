-- 005_triggers.sql
-- DB-level triggers for data integrity and audit

-- ─── updated_at auto-maintenance ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_sources_updated_at
    BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_research_updated_at
    BEFORE UPDATE ON research_notes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_positions_updated_at
    BEFORE UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ─── Allocation ceiling enforcement (HARD RULE) ───────────────────────────────
CREATE OR REPLACE FUNCTION enforce_max_allocation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.current_allocation > 0.12 THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: current_allocation (%) exceeds max 12%%. Allocation cannot exceed 0.12.',
            NEW.current_allocation;
    END IF;
    IF NEW.target_allocation IS NOT NULL AND NEW.target_allocation > 0.12 THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: target_allocation (%) exceeds max 12%%. Allocation cannot exceed 0.12.',
            NEW.target_allocation;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_enforce_max_allocation
    BEFORE INSERT OR UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION enforce_max_allocation();

-- ─── Minimum initiation size enforcement (HARD RULE) ─────────────────────────
CREATE OR REPLACE FUNCTION enforce_min_initiation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- Only applies when moving from 0 to non-zero (initiation)
    IF OLD.current_allocation = 0.00 AND NEW.current_allocation > 0.00
       AND NEW.current_allocation < 0.02 THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: Initiation size (%) is below minimum 2%%. Must initiate at >= 0.02.',
            NEW.current_allocation;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_enforce_min_initiation
    BEFORE UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION enforce_min_initiation();

-- ─── Thesis required before open (HARD RULE) ─────────────────────────────────
CREATE OR REPLACE FUNCTION enforce_thesis_required()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'open' AND (NEW.thesis_locked = FALSE OR NEW.thesis IS NULL) THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: Position cannot be opened without a locked thesis. Set thesis and thesis_locked=TRUE first.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_enforce_thesis_required
    BEFORE INSERT OR UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_required();

-- ─── Invalidation conditions required before open (HARD RULE) ────────────────
CREATE OR REPLACE FUNCTION enforce_invalidation_required()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'open' AND (NEW.invalidation_conditions IS NULL OR LENGTH(TRIM(NEW.invalidation_conditions)) = 0) THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: Position cannot be opened without documented invalidation conditions.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_enforce_invalidation_required
    BEFORE INSERT OR UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION enforce_invalidation_required();

-- ─── Allocation ladder enforcement (HARD RULE) ───────────────────────────────
-- Ladder: 0 → 0.02 → 0.04 → 0.07 → 0.10 → 0.12 (no skipping when adding)
CREATE OR REPLACE FUNCTION enforce_allocation_ladder()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    ladder NUMERIC[] := ARRAY[0.00, 0.02, 0.04, 0.07, 0.10, 0.12];
    from_step INTEGER;
    to_step INTEGER;
    i INTEGER;
BEGIN
    -- Only enforce when increasing allocation
    IF NEW.current_allocation <= OLD.current_allocation THEN
        RETURN NEW;
    END IF;

    -- Find from_step and to_step in ladder
    from_step := -1;
    to_step := -1;
    FOR i IN 1..array_length(ladder, 1) LOOP
        IF ABS(ladder[i] - OLD.current_allocation) < 0.001 THEN
            from_step := i;
        END IF;
        IF ABS(ladder[i] - NEW.current_allocation) < 0.001 THEN
            to_step := i;
        END IF;
    END LOOP;

    -- If new allocation is not a ladder step, reject
    IF to_step = -1 THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: Allocation % is not a valid ladder step. Valid steps: 0%%, 2%%, 4%%, 7%%, 10%%, 12%%.',
            NEW.current_allocation;
    END IF;

    -- If skipping a step on the way up, reject
    IF from_step != -1 AND to_step > from_step + 1 THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: Allocation ladder step skipped. Cannot jump from % to %. Must follow sequence: 2%% → 4%% → 7%% → 10%% → 12%%.',
            OLD.current_allocation, NEW.current_allocation;
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_enforce_allocation_ladder
    BEFORE UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION enforce_allocation_ladder();

-- ─── model_rules is immutable (hard rules cannot be modified) ─────────────────
CREATE OR REPLACE FUNCTION block_model_rules_modification()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.rule_type = 'hard' THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: Hard model rules are immutable and cannot be modified via the database.';
    END IF;
    IF TG_OP = 'DELETE' AND OLD.rule_type = 'hard' THEN
        RAISE EXCEPTION
            'HARD RULE VIOLATION: Hard model rules cannot be deleted.';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_block_model_rules_modification
    BEFORE UPDATE OR DELETE ON model_rules
    FOR EACH ROW EXECUTE FUNCTION block_model_rules_modification();
