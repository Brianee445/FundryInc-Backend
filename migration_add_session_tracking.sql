-- Single-device login enforcement: every login/signup overwrites this,
-- which invalidates any token issued before it (see get_current_user in
-- app/dependencies.py). NULL for existing users until they next log in —
-- intentional, so this doesn't force a mass logout on deploy day.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS current_session_id UUID;
