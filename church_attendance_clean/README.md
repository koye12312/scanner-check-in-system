# Scanner+Check-In System (Flask + QR Codes)

A complete check-in/out system for churches, schools, or small events.  
Built with **Flask** and **QR code scanning**, it allows fast registration, secure attendance tracking, parent–child check-ins, and real-time logging with admin control.

---

## ✨ Features

- **QR-based registration**
  - Adults/parents/children register once, auto-generating a QR code
  - Prevents duplicate registrations (case-insensitive name matching)
- **Check-In / Check-Out**
  - Fast QR scanning for arrivals & departures
  - Duplicate prevention (same person/child cannot be checked in twice)
  - Parent–child linking: only unscanned children appear for a second parent
- **Admin Dashboard**
  - PIN-protected `/dashboard` route
  - View live attendance logs
  - Edit or delete log entries directly
  - Export logs to CSV
- **Data Handling**
  - CSV storage (`data/registrations.csv`, `data/logs.csv`)
  - No external database required
- **Push Notifications (Optional)**
  - Web-push support using VAPID keys (`generate_vapid_keys.py`)
  - Instant real-time updates when new logs are added
- **Deployment**
  - Works locally or on a server
  - PyInstaller packaging included → portable `.exe` for Windows

---

## 🛠️ Installation (Local)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
