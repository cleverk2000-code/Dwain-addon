# Dwain-addon

HACS Integration companion for [Dwains Lovelace Dashboard 3.0 fork](https://github.com/cleverk2000-code/dwains-lovelace-dashboard/tree/3.0), **not Next**. No Supervisor add-on or Docker required. Requires HA 2026.5+ and the fork with configuration_runtime.py and load_dashboard.invalidate_dashboard_cache. Does not modify or replace the main fork.

## Installation
1. HACS > Custom repositories > add https://github.com/cleverk2000-code/Dwain-addon as Integration.
2. Download the repository (main), restart Home Assistant.
3. Settings > Devices & services > Add integration > Dwain-addon.
4. Keep existing Dwains Lovelace installed and enabled.

## API (administrator only)
Use your existing authenticated HA HTTPS API session. For local development, export HA_URL and HA_TOKEN in your own terminal; never commit or share an admin token. No new public listener, GitHub bridge, AI credential, or anonymous endpoint is installed. ChatGPT connectivity is a separate step.

GET /api/dwain_addon/layout returns the HA area/entity registry, current settings, revision hash, pending transaction ID and last 100 backup IDs.
POST /api/dwain_addon/layout accepts JSON and actions below.

Example preview (IDs and revision must come from GET):
```json
{"action":"preview","revision":"CURRENT_REVISION","patch":{"areas":{"living_room":{"sort_order":1}},"entities":{"light.living_room":{"sort_order":1,"hidden_in_area":false,"col_span":"2"}}}}
```
Review the changes, then send the same object with action=apply. Always use the original revision from the preview. If other edits intervened, read and preview again. Existing fields not in the patch are preserved. A no-op creates no backup. Rank values are positive integers; use full sibling rankings when reordering to avoid equal ranks (the existing dashboard uses 99 for unconfigured entries). Mobile span 1–2, large 1–3, XL 1–4; sizes apply wherever the fork consumes those entity settings.

Allowed areas: sort_order, grouped_sort_order, disabled, hide_icon, icon.
Allowed entities: sort_order, grouped_sort_order, favorite_sort_order, devices_sort_order, devices_grouped_sort_order, hidden, hidden_in_area, excluded, disabled, favorite, friendly_name, row_span, col_span, row_span_lg, col_span_lg, row_span_xl, col_span_xl.
hidden_in_area is independent from hidden. Device/area assignments, automations, physical device states, arbitrary custom-card code, and the main Lovelace source are not changed.

Restore: POST {"action":"restore_preview","revision":"CURRENT_REVISION","backup_id":"ID"} then action=restore. Refuses to overwrite newer changes in any affected file. Restore itself makes a backup.
Interrupted transaction: GET reports pending; POST {"action":"recover","backup_id":"PENDING_ID"} rolls back only when each file still matches a known before/after version. If an external writer changed the file, manual recovery is required. Raw backup records are in /config/.storage/dwain_addon_layout/ (not web served). Backups are retained; manage old backups manually with HA stopped and no pending transaction. YAML comments/formatting may change on edited files; backup/restore preserves exact original bytes. Duplicate keys, custom tags, unsupported YAML and invalid IDs fail closed. Shared lock coordinates with the fork's dashboard edits; unrelated external file editors must not write concurrently.

## Test scope
Run python -m unittest discover -s tests -v after installing PyYAML. CI covers isolated disk transactions; live HA rendering/authentication and actual user installation need separate validation.
