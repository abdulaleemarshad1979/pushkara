# Godavari Pushkaralu 2027 — Setup & Run Manual (v13)

## ⚠️ IMPORTANT: Correct URLs After Starting Docker

```
Admin Panel: http://localhost:8088/admin.html  ✅ CORRECT
User Page:   http://localhost:8088/            ✅ CORRECT

WRONG URLs (will NOT work):
  http://localhost:8000/admin.html  ❌ WRONG - raw API, no HTML
  http://localhost:8000/            ❌ WRONG - raw API
```

Port 8000 is the internal FastAPI backend. It is **NOT** exposed to your browser.
All traffic goes through Nginx on port 80.

---

# Godavari Pushkaralu 2027 — Setup & Run Manual (v9)

## What Changed in v9

- **Admin portal login removed** — Admin access is handled externally by government officials. This codebase only exposes volunteer authentication.
- **Fixed import errors** — `app.core.auth`, `app.core.pg_store`, and `app.core.storage` now correctly exist in the `app/core/` package.
- **`/admin/login` endpoint removed** — No admin credentials are accepted or stored by this API.
- **`/ws/admin` WebSocket removed** — Volunteer WebSocket (`/ws/volunteer`) remains fully functional.
- **`/admin/stats` renamed to `/volunteer/stats`** — Requires a valid volunteer JWT.
- All `require_admin` guards on operational routes (resolve issue, resolve SOS, assign SOS, update crowd, manage contacts, manage medical) are now `require_volunteer` — logged-in volunteers can perform all operational actions.

---

## Prerequisites

| Tool | Minimum Version | Check |
|------|----------------|-------|
| Python | 3.11+ | `python --version` |
| PostgreSQL | 15+ | `psql --version` |
| Redis | 7+ | `redis-server --version` |
| pip | latest | `pip --version` |

**OR** use Docker (recommended — skips all manual DB setup):

| Tool | Check |
|------|-------|
| Docker Desktop | `docker --version` |
| Docker Compose | `docker compose version` |

---

## Option A — Docker (Recommended)

Docker spins up FastAPI + PostgreSQL + Redis in one command. No manual DB configuration needed.

### Step 1 — Configure Environment

```bash
cp .env.example .env
```

Open `.env` and set these required values:

```env
# Generate a secure JWT key:
# python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY=<paste_generated_key_here>
JWT_EXPIRY_HOURS=8

# PostgreSQL password (Docker will create the DB with this)
POSTGRES_PASSWORD=choose_a_strong_password

# Leave Redis as-is for Docker
REDIS_URL=redis://redis:6379/0
```

Leave S3 variables blank for local development (images will save to disk under `uploads/`).

### Step 2 — Start the Stack

```bash
docker compose up --build
```

On first run, Docker will:
1. Build the FastAPI image
2. Start PostgreSQL and apply `db/schema.sql` automatically
3. Start Redis
4. Start the FastAPI server on **http://localhost:8000**

### Step 3 — Verify

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{"status": "ok", "instance": "api-<pid>", ...}
```

### Stop

```bash
docker compose down          # stop containers
docker compose down -v       # stop + delete volumes (resets DB)
```

---

## Option B — Local Development (No Docker)

### Step 1 — Install PostgreSQL

#### Windows
Download and install from https://www.postgresql.org/download/windows/

After install, open **pgAdmin** or **psql** and create the database:

```sql
CREATE DATABASE pushkaralu;
CREATE USER pushkaralu_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE pushkaralu TO pushkaralu_user;
```

#### macOS
```bash
brew install postgresql@15
brew services start postgresql@15
psql postgres -c "CREATE DATABASE pushkaralu;"
psql postgres -c "CREATE USER pushkaralu_user WITH PASSWORD 'your_password';"
psql postgres -c "GRANT ALL PRIVILEGES ON DATABASE pushkaralu TO pushkaralu_user;"
```

#### Ubuntu / Debian
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo -u postgres psql -c "CREATE DATABASE pushkaralu;"
sudo -u postgres psql -c "CREATE USER pushkaralu_user WITH PASSWORD 'your_password';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE pushkaralu TO pushkaralu_user;"
```

### Step 2 — Apply the Database Schema

```bash
psql -U pushkaralu_user -d pushkaralu -f db/schema.sql
```

This creates all tables: `ghats`, `volunteers`, `sos_alerts`, `issues`, `lost_persons`, `emergency_contacts`, `medical_facilities`, `crowd_snapshots`, `audit_events`.

Verify tables were created:
```bash
psql -U pushkaralu_user -d pushkaralu -c "\dt"
```

### Step 3 — Install Redis

#### Windows
Download from https://github.com/microsoftarchive/redis/releases and run `redis-server.exe`.

#### macOS
```bash
brew install redis
brew services start redis
```

#### Ubuntu / Debian
```bash
sudo apt install redis-server
sudo systemctl start redis-server
```

Verify Redis is running:
```bash
redis-cli ping
# Expected: PONG
```

### Step 4 — Set Up Python Environment

```bash
# Create virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Step 5 — Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
# PostgreSQL connection string
DATABASE_URL=postgresql://pushkaralu_user:your_password@localhost:5432/pushkaralu

# Redis
REDIS_URL=redis://localhost:6379/0

# JWT — generate with: python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY=<your_generated_key>
JWT_EXPIRY_HOURS=8
```

### Step 6 — Run the API Server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

- `--reload` enables hot-reload on file changes (development only, remove in production)
- API runs at **http://localhost:8000**
- Interactive docs at **http://localhost:8000/docs**

---

## Adding Volunteers to the Database

Volunteers are the only users who can log in. Add them via SQL or via the API.

### Option 1 — Directly in PostgreSQL

Generate a bcrypt hash for the password first:
```bash
python -c "from passlib.context import CryptContext; ctx = CryptContext(schemes=['bcrypt']); print(ctx.hash('volunteer_password'))"
```

Then insert:
```sql
INSERT INTO volunteers (id, name, username, password_hash, phone, zone, status)
VALUES (
    gen_random_uuid()::TEXT,
    'Ravi Kumar',
    'ravi.kumar',
    '$2b$12$<the_hash_you_generated>',
    '9876543210',
    'Zone-A',
    'available'
);
```

### Option 2 — Via sample_data.json

Edit `data/sample_data.json` and add entries to the `"volunteers"` array:

```json
{
  "id": "vol-001",
  "name": "Ravi Kumar",
  "username": "ravi.kumar",
  "password": "plaintext_password_will_be_hashed_on_first_login",
  "phone": "9876543210",
  "zone": "Zone-A",
  "status": "available"
}
```

Plain-text passwords in `sample_data.json` are **automatically bcrypt-hashed on first login** — this is safe for initial seeding only.

---

## Volunteer Login Flow

**Endpoint:** `POST /volunteer_login`

```bash
curl -X POST http://localhost:8000/volunteer_login \
  -F "username=ravi.kumar" \
  -F "password=volunteer_password"
```

**Response:**
```json
{
  "success": true,
  "token": "eyJ...",
  "volunteer": {
    "id": "vol-001",
    "name": "Ravi Kumar",
    "zone": "Zone-A",
    "status": "available"
  }
}
```

**Using the token** — include in all protected requests:
```
Authorization: Bearer eyJ...
```

Tokens expire after `JWT_EXPIRY_HOURS` (default: 8 hours). The volunteer must log in again to get a new token.

---

## Protected Endpoints (Volunteer Auth Required)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/resolve_issue/{id}` | POST | Mark an issue resolved |
| `/resolve_sos/{id}` | POST | Resolve an SOS alert |
| `/assign_sos/{id}` | POST | Assign SOS to a volunteer |
| `/update_crowd/{ghat_id}` | POST | Update crowd level at a ghat |
| `/contacts` | POST | Add emergency contact |
| `/contacts/{id}` | DELETE | Remove emergency contact |
| `/medical` | POST | Add medical facility |
| `/medical/{id}` | DELETE | Remove medical facility |
| `/volunteer/stats` | GET | Get operational statistics |
| `/volunteer/{id}` | PUT | Update volunteer profile |
| `/lost/{id}` | PUT | Update lost person status |

---

## Object Storage (Image Uploads)

By default, uploaded images are saved locally in the `uploads/` folder.

To use **AWS S3** or **Cloudflare R2**, fill in `.env`:

```env
S3_BUCKET_NAME=your-bucket-name
S3_ACCESS_KEY_ID=your-access-key
S3_SECRET_ACCESS_KEY=your-secret-key
S3_REGION=ap-south-1          # AWS region, or "auto" for R2
S3_ENDPOINT_URL=               # Leave blank for AWS. R2 example: https://<account_id>.r2.cloudflarestorage.com
S3_PUBLIC_BASE_URL=            # CDN URL, e.g. https://cdn.pushkaralu.gov.in
S3_ACL=                        # "public-read" for AWS. Leave blank for R2.
```

---

## Production Checklist

- [ ] Set a strong `JWT_SECRET_KEY` (32+ random bytes)
- [ ] Set a strong `POSTGRES_PASSWORD`
- [ ] Set a strong `REDIS_PASSWORD` if Redis is exposed to a network
- [ ] Remove `--reload` flag from uvicorn
- [ ] Run behind Nginx (config provided in `infrastructure/nginx/nginx.conf`)
- [ ] Enable HTTPS/TLS on Nginx
- [ ] Configure S3/R2 for persistent image storage (local `uploads/` resets on redeploy)
- [ ] Rotate `JWT_SECRET_KEY` to invalidate all existing tokens when deploying

---

## Troubleshooting

**`Import "app.core.auth" could not be resolved`**
→ Fixed in v9. The files `app/core/auth.py`, `app/core/pg_store.py`, and `app/core/storage.py` now exist. If you see this in your IDE, reload the Python interpreter or restart VS Code.

**`redis.exceptions.ConnectionError`**
→ Redis is not running. Start it: `redis-server` (local) or check Docker: `docker compose ps`.

**`asyncpg.exceptions.ConnectionDoesNotExistError`**
→ PostgreSQL is not running or `DATABASE_URL` is wrong. Check your `.env`.

**Volunteer login returns 401**
→ Check the username (case-insensitive, spaces trimmed) and password. Check that the volunteer exists in `DB["volunteers"]` by visiting `GET /get_volunteers`.

**Images not uploading**
→ If S3 env vars are blank, images save to `uploads/`. Make sure the `uploads/` folder exists and is writable. Docker creates it automatically.

---

## Project Structure

```
pushkaralu_v9/
├── main.py                    # FastAPI application & all routes
├── auth.py                    # (root copy — do not use; use app/core/auth.py)
├── pg_store.py                # (root copy — do not use; use app/core/pg_store.py)
├── storage.py                 # (root copy — do not use; use app/core/storage.py)
├── app/
│   ├── core/
│   │   ├── auth.py            # ✅ JWT auth — volunteer login only
│   │   ├── pg_store.py        # ✅ PostgreSQL async store
│   │   ├── storage.py         # ✅ S3/R2 image upload
│   │   ├── redis_manager.py   # Redis cache, pub/sub, streams
│   │   ├── risk_engine.py     # Physics-based crowd risk engine
│   │   ├── ws_manager.py      # WebSocket manager
│   │   └── ai_predictor.py    # AI crowd prediction
│   ├── healing/
│   │   └── orchestrator.py    # Self-healing guardian loops
│   └── workers/
│       ├── cctv_worker.py     # CCTV crowd detection worker
│       └── db_writer.py       # Async DB write worker
├── db/
│   └── schema.sql             # PostgreSQL schema (apply once)
├── data/
│   └── sample_data.json       # Seed data for development
├── infrastructure/
│   └── nginx/nginx.conf       # Nginx reverse proxy config
├── docker-compose.yml         # Full stack Docker setup
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
└── SETUP_MANUAL.md            # This file
```
