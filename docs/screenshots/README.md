# Screenshots — capture checklist

This folder is referenced by the root `README.md` but images aren't included here yet — add your own, following the redaction notes below. (I attempted to capture these automatically via a browser tool this session and hit an environment limitation — the render pane wouldn't composite frames for a screenshot — so this is a manual step, same as it was for the sibling `starlink tracker` project.)

| File to create | What to capture | Redaction needed? |
|---|---|---|
| `beacon_sheet.png` | Open your live Beacon Google Sheet. Scroll/select so Company, Title, Location, Visa Flag, and Initial Fit Score columns are visible for several rows. | **Yes** — crop or blur the **Decision** and **My Decision** columns (your actual approve/deny/reject choices), and blur **Salary Range** if you'd rather not show real posted figures. Company/Title/Visa Flag are fine to show as-is — they're facts about the posting, not your personal choices. |
| `job_log_sheet.png` | Open your Job Log ("Filtered") sheet. Capture a few rows with their "Reason for Rejection" visible. | Same as above — blur the Decision/My Decision columns if populated on any shown row. |
| `database.png` | Run `sqlite3 job_search.db` (or open the file in DB Browser for SQLite / a VS Code SQLite extension) and screenshot either `.schema jobs` or a `SELECT company_id, title, location, visa_flag, cloud_platforms FROM jobs LIMIT 10;`. | Keep the `SELECT` limited to non-personal columns — don't include `decision_processed_at`, or anything from `fit_scores`/`resume_feedback` tied to your own review notes. |
| `source_greenhouse.png` | Navigate to any real public Greenhouse board, e.g. `https://job-boards.greenhouse.io/workato`, and screenshot the open-roles list. | **None** — this is already public. |

**Quick way to capture on Windows**: `Win+Shift+S` for a region screenshot, or the Snipping Tool. Paste into Paint/Photos, crop/blur the sensitive column, save as PNG into this folder with the exact filename from the table above.
