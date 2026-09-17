-- 010_api_lockdown.sql — Revoke all Supabase API-role access to raw tables.
--
-- Current state (audited 2026-09-17): RLS is ON with zero policies on all 29
-- tables (deny-by-default, good) BUT anon/authenticated hold full table grants.
-- RLS blocks them today; one future CREATE POLICY or RLS toggle would expose
-- everything. This migration makes the safe state explicit and permanent.
--
-- APPLY REQUIRES OWNER APPROVAL — production permission change.
-- After applying, verify from outside with the publishable key: every table
-- read must return "permission denied", not rows and not an RLS-empty 200.

-- 1. Strip all existing grants from the API roles on every table & sequence.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;

-- 2. Stop future tables from being auto-granted (default privileges are why
--    every new table sprouted anon grants in the first place).
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES    FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated;

-- 3. Belt-and-braces: ensure RLS stays on everywhere (idempotent).
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT c.relname FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind = 'r'
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.relname);
  END LOOP;
END $$;

-- anon and authenticated retain USAGE on schema public (needed to resolve the
-- views granted in 011); they can see names, read nothing.
GRANT USAGE ON SCHEMA public TO anon, authenticated;
