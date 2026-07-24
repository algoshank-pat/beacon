-- Third, best-effort industry source (TinyFish Search API, free) for
-- companies FMP and StartupHub both left blank -- see app.tinyfish's module
-- docstring for the full design, including the two safeguards (multi-source
-- agreement, audit trail) added after live testing found real name-collision
-- risk in raw search results (the same failure mode that already burned FMP
-- once on "Kong").
--
-- industry_source_url: the URL of the corroborating result that backed the
-- extracted `industry` value, so a wrong match is visible/auditable rather
-- than a silent black box -- same principle as dol_lca_employer_name.
--
-- tinyfish_last_checked: independent "checked" tracker, same pattern as
-- startuphub_last_checked/financial_data_last_checked -- a company with no
-- corroborated match still gets stamped so it isn't re-searched forever.
ALTER TABLE companies ADD COLUMN industry_source_url TEXT;
ALTER TABLE companies ADD COLUMN tinyfish_last_checked TIMESTAMP;
