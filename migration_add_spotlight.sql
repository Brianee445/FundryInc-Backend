-- founder_profile_views is a brand-new table, so app/main.py's
-- Base.metadata.create_all() on startup creates it automatically — no
-- migration needed for that one. founder_profiles is an existing table
-- though, so its new column needs this manual ALTER:

ALTER TABLE founder_profiles
    ADD COLUMN IF NOT EXISTS is_spotlighted BOOLEAN NOT NULL DEFAULT false;
