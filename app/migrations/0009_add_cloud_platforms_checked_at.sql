-- app.cloud_platforms_refresh's "check once" tracking column, same pattern
-- as salary_checked_at (migration 0006) / link_checked_at (migration 0005).
ALTER TABLE jobs ADD COLUMN cloud_platforms_checked_at TIMESTAMP;
