# Godavari Pushkaralu 2027

A real-time crowd management and emergency response system for the Godavari Pushkaralu festival (July 11–22, 2027, Rajahmundry).

## What it does

- Shows live crowd levels at each ghat on a map
- Lets pilgrims raise SOS alerts and report issues
- Volunteers log in to accept and resolve SOS/issues
- Tracks lost persons with search
- Admin dashboard for full command and control
- CCTV integration using YOLO for automatic crowd counting

## Tech Stack

- **Backend** — FastAPI (Python) with 4 load-balanced instances
- **Cache & Realtime** — Redis (pub/sub + streams)
- **Database** — PostgreSQL
- **Reverse Proxy** — NGINX
- **Frontend** — Plain HTML/JS (no build step needed)

## Run Locally

**Requirements:** Docker and Docker Compose installed.

```bash
# 1. Clone the repo
git clone <repo-url>
cd Pushkara-main

# 2. Set up environment
cp .env.example .env
# Open .env and set: POSTGRES_PASSWORD, JWT_SECRET_KEY, ADMIN_API_KEY, ADMIN_PASSWORD

# 3. Start everything
docker compose up -d --build

# 4. Open in browser
# Pilgrim view:   http://localhost:8088/user.html
# Volunteer login: http://localhost:8088/index.html
# Admin panel:    http://localhost:8088/admin.html
```

To stop:
```bash
docker compose down
```

To view logs:
```bash
docker compose logs -f
```

## Default Login

- **Admin panel key** — value of `ADMIN_API_KEY` in your `.env`
- **Volunteer login** — create a volunteer via the admin panel first