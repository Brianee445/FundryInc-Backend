ALTER TABLE waitlist_entries ADD COLUMN IF NOT EXISTS launch_notified_at TIMESTAMPTZ;
