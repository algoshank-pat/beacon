# Screenshots

| File | Status | What it shows |
|---|---|---|
| `beacon_sheet.png` | ✅ Added | Live Beacon sheet — Company/Title/Industry/Location/Visa Flag/Salary Range/Decision columns. Decision/My Decision columns visible are unset defaults (`Pending`/`New`), not personal choices, so no redaction was needed. |
| `database_schema.png` | ✅ Added | DB Browser for SQLite's "Database Structure" tab — all 9 tables and 8 indices. |
| `database_data.png` | ✅ Added | DB Browser's "Browse Data" tab on the `companies` table — real research data (industry, board URL, employee count), no personal fields. |
| `source_greenhouse.png` | ✅ Added | Anthropic's real, fully public Greenhouse job board — no redaction needed, it's already public. |
| `job_log_sheet.png` | Not added yet | The Job Log sheet with a few excluded jobs and their rejection reasons. Same redaction rule as `beacon_sheet.png` — blur/crop the Decision/My Decision columns only if a row shows a real choice you made, not a default value. |

The root `README.md`'s screenshot grid currently references the four "Added" files above. If you add `job_log_sheet.png` later, update that grid to include it.
