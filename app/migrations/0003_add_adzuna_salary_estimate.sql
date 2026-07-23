-- Adzuna's salary_min/salary_max is an algorithmic estimate, not what the
-- employer actually posted (confirmed wrong on a real listing -- see
-- app.salary_extraction). jobs.salary_min/salary_max is reserved for the
-- real, JD-text-extracted range and gets left blank when the JD doesn't
-- state one explicitly, which is most of the time. These columns capture
-- Adzuna's estimate permanently and separately, so the Beacon sheet can
-- show it as a clearly-labeled fallback instead of leaving salary blank
-- whenever the real number can't be extracted.
ALTER TABLE jobs ADD COLUMN adzuna_salary_min INTEGER;
ALTER TABLE jobs ADD COLUMN adzuna_salary_max INTEGER;
