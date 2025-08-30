# Church Attendance System (Flask + QR Codes)

A lightweight check‑in/out system built with Flask. It supports QR-based registration, duplicate‑check prevention, parent/child logic, and an admin dashboard for logs and exports.

## Features
- Register members (adult/parent/child) and auto‑generate QR codes
- Fast check‑in/check‑out (prevents duplicates)
- Parent‑child linking (only unscanned children appear for a second parent)
- Admin dashboard: view, search, and export logs
- CSV storage (no external DB), simple deployment
- PyInstaller packaging (optional)

## Security & Privacy
- **No personal data in this repo.** `data/` contains empty sample CSVs.
- `static/qrcodes/` is empty (placeholder only).

## Getting Started (Local)

```bash
# 1) Create & activate a virtual env (recommended)
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2) Install dependencies
pip install -r requirements.txt


# 4) Run the app
python app.py
```

The app serves templates from `/templates` and static assets from `/static`. QR codes will be generated to `static/qrcodes/` at registration time.

## Project Structure
```
app.py
templates/
static/
  qrcodes/        # empty; generated at runtime
data/
  registrations.csv  # sample headers only
  logs.csv           # sample headers only
requirements.txt
.gitignore
vapid_keys_example.py
```

## Production Notes
- Use a proper SMTP account for email (the code uses standard `smtplib`).
- Protect admin views (PIN or auth) if you deploy publicly.
- If packaging with PyInstaller, keep build artifacts out of the repo.