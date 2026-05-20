# HTML File Locations

Both root `/` and `/dashboards/` contain identical, fully patched files.
nginx mounts from root — dashboards/ is a backup copy.

| File       | Root `/`  | `/dashboards/` |
|------------|-----------|----------------|
| index.html | ✅ File   | ✅ File        |
| admin.html | ✅ File   | ✅ File        |
| user.html  | ✅ File   | ✅ File        |
| config.js  | ✅ File   | ✅ File        |

## ⚠️ What was broken in v17 (now fixed)

The root `admin.html`, `index.html`, and `user.html` were accidentally
**empty folders** instead of files — caused by dragging/moving in Windows Explorer
or VS Code. Docker mounted them as directories, nginx got a 404, FastAPI
returned `{"detail":"Not Found"}`.

**Fix applied:** deleted the empty folders, copied real files from dashboards/ to root.

## Auth fixes also applied (from v16 patch session)
- index.html: JWT token stored + authFetch() on all volunteer requests
- admin.html: X-Admin-Key header on all api() calls
- config.js:  ADMIN_API_KEY constant
- user.html:  429 rate-limit handled in triggerSOS()
