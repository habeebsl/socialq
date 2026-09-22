-- Close the PostgREST door.
--
-- Supabase exposes every table in `public` over its REST API and grants the
-- `anon` and `authenticated` roles full DML by default. The anon key is
-- designed to be public -- it ships in browsers -- so those defaults mean
-- anyone holding it can INSERT into posts and targets, and the worker will
-- publish whatever it finds to a real account. That is remote-controlled
-- posting, not a hardening nicety.
--
-- socialq does not use PostgREST at all. It connects as the table owner over
-- a normal Postgres connection, and an owner bypasses RLS, so revoking these
-- grants costs us nothing.
--
-- If socialq is ever pointed at a non-owner role, RLS will start applying to
-- it and every query will return nothing. Add policies at that point rather
-- than disabling this.

DO $$
BEGIN
  -- These roles exist only on Supabase; local Postgres and CI have neither.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM authenticated;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      REVOKE ALL ON TABLES FROM authenticated;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      REVOKE ALL ON SEQUENCES FROM authenticated;
  END IF;
END
$$;

-- Defence in depth: even if a grant comes back, RLS with no policies denies
-- every non-owner role. It is also what clears Supabase's "Unrestricted"
-- badge, so the dashboard stops reporting a problem that is fixed.
ALTER TABLE projects              ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts              ENABLE ROW LEVEL SECURITY;
ALTER TABLE media                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE targets               ENABLE ROW LEVEL SECURITY;
ALTER TABLE attempts              ENABLE ROW LEVEL SECURITY;
ALTER TABLE publisher_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations     ENABLE ROW LEVEL SECURITY;
