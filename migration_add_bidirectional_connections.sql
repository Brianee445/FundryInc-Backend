-- investor_profiles is a brand-new table — app/main.py's
-- Base.metadata.create_all() creates it automatically on startup, no
-- migration needed for it.
--
-- connection_requests is existing, so its new column needs a manual ALTER.
-- Every pre-existing row was investor-initiated (this feature didn't exist
-- before), so the DEFAULT backfills historical rows correctly.
ALTER TABLE connection_requests
    ADD COLUMN IF NOT EXISTS initiator VARCHAR NOT NULL DEFAULT 'investor';
