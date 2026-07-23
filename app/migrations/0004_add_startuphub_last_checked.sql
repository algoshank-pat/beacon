-- Splits enrichment's single "checked" flag into two independent ones so
-- the free, unlimited-quota StartupHub.ai pass can run against the full
-- backlog every time (no daily cap needed) while the FMP pass -- the only
-- source for employee_count/company_type/funding_stage/revenue_or_valuation,
-- and the one actually bound by a real quota (250 requests/day) -- keeps
-- its own capped, slower cadence via financial_data_last_checked. Before
-- this, one shared timestamp meant a company already checked against FMP
-- (e.g. a confirmed public match) could never be revisited for StartupHub's
-- hq_location/founded_year/industry fields, and vice versa.
ALTER TABLE companies ADD COLUMN startuphub_last_checked TIMESTAMP;
