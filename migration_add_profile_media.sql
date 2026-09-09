-- app/main.py runs Base.metadata.create_all() on startup, which will
-- auto-create the new `messages` table for you — no migration needed there.
-- It does NOT alter existing tables, though, so founder_profiles needs this
-- run manually before/after deploying:

ALTER TABLE founder_profiles
    ADD COLUMN IF NOT EXISTS profile_picture_url TEXT,
    ADD COLUMN IF NOT EXISTS gallery_image_urls TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS startup_link TEXT;
