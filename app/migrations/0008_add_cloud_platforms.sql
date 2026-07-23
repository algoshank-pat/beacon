-- Computed once at ingestion time by app.cloud_platforms.resolve_cloud_platforms
-- from the job's title+description text. A comma-separated subset of
-- "AWS, GCP, Azure" (always in that fixed order), or NULL if none mentioned.
ALTER TABLE jobs ADD COLUMN cloud_platforms TEXT;
