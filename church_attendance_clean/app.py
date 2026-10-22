import sys
import urllib.parse
import re
from flask import Flask, render_template, request, redirect, session, url_for, jsonify, send_from_directory
import csv, os, qrcode
from datetime import datetime, timedelta
import webbrowser
import threading
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from dateutil.relativedelta import relativedelta
from pathlib import Path
from collections import defaultdict
import time
import secrets
from functools import wraps
from html import escape
RECENT_CHECKINS = defaultdict(float)
RESCAN_COOLDOWN_SECONDS = 8


app = Flask(__name__)
# A fixed SECRET_KEY keeps admins logged in across restarts; without one we
# generate a random key so the app never runs with a guessable default.
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
ADMIN_PIN = os.getenv('ADMIN_PIN', '4321')
ORG_NAME = os.getenv('ORG_NAME', 'Church Check-In')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

if getattr(sys, 'frozen', False):
    app.template_folder = os.path.join(sys._MEIPASS, 'templates')
    app.static_folder = os.path.join(sys._MEIPASS, 'static')


@app.context_processor
def inject_globals():
    """Values every template can use."""
    return {
        "org_name": ORG_NAME,
        "is_admin": bool(session.get("authenticated")),
    }


def login_required(view):
    """Redirect to the admin login unless the session is authenticated."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/admin-login")
        return view(*args, **kwargs)
    return wrapped


def notice(title, message, kind="error", status=400, action_href="/scan", action_label="Back to scanner"):
    """Render a friendly full-page message instead of a bare text response."""
    return render_template(
        "message.html",
        title=title,
        message=message,
        kind=kind,
        action_href=action_href,
        action_label=action_label,
    ), status


# Email configuration — set these as environment variables to enable emails.
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")

def send_qr_email(recipient_email, name, qr_path):
    """Email the QR code. Returns True if the email was sent."""
    if not (EMAIL_HOST and EMAIL_USER and EMAIL_PASS):
        print("ℹ️  Email not configured (EMAIL_HOST/EMAIL_USER/EMAIL_PASS); skipping QR email.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = EMAIL_USER
        msg["To"] = recipient_email
        msg["Subject"] = f"Welcome to {ORG_NAME} – your check-in QR code"

        # HTML email content
        safe_name = escape(name)
        safe_org = escape(ORG_NAME)
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #18213A; line-height: 1.6;">
            <h2 style="color:#18213A;">Hi {safe_name},</h2>
            <p>Welcome to <strong>{safe_org}</strong>.</p>
            <p>Your check-in QR code is attached to this email.</p>
            <p>Show it at the welcome desk when you arrive and it will check you in straight away.</p>
            <p style="margin-top: 20px;">See you soon,<br><strong>{safe_org}</strong></p>
        </body>
        </html>
        """

        # Attach HTML content
        msg.attach(MIMEText(html_body, "html"))

        # Attach QR code image
        with open(qr_path, "rb") as f:
            img = MIMEImage(f.read())
            img.add_header("Content-Disposition", "attachment", filename=os.path.basename(qr_path))
            msg.attach(img)

        # Send email
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, recipient_email, msg.as_string())

        print(f"✅ QR email sent to {recipient_email}")
        return True

    except Exception as e:
        print(f"❌ Error sending email to {recipient_email}: {e}")
        return False


# Path configuration
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

DATA_DIR = Path(APP_DIR) / "data"
QR_FOLDER = Path(APP_DIR) / "static" / "qrcodes"
REG_CSV = DATA_DIR / "registrations.csv"
LOG_CSV = DATA_DIR / "logs.csv"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)

# --------------------- UTILS ---------------------

def get_registered_parents():
    parents = set()
    if not os.path.exists(REG_CSV):
        return []
    
    with open(REG_CSV, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 10: continue
            if row[5] in ["Parent", "Adult"]:
                parents.add(f"{row[0]} {row[1]}")
    return sorted(list(parents))

def normalize_name(name):
    """Capitalise the first letter of each word but keep the rest as typed,
    so "mary-jane o'neill" -> "Mary-jane O'neill" and "McDonald" stays "McDonald"."""
    return " ".join(part[:1].upper() + part[1:] for part in name.strip().split())

def name_has_number(name):
    return any(char.isdigit() for char in name)

def already_registered(full_name):
    if not os.path.exists(REG_CSV): return False
    with open(REG_CSV, newline="") as f:
        return any(f"{row[0]} {row[1]}".lower() == full_name.lower() 
                  for row in csv.reader(f) if len(row) >= 2)

# Replace with this:
def is_checked_in(name):
    """Check if a specific person is checked in (not based on children)."""
    if not os.path.exists(LOG_CSV):
        return False

    today = str(datetime.now().date())
    with open(LOG_CSV, newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 5:
                log_name = row[0].strip().lower()
                log_date = row[2].strip() if len(row) > 2 else ""
                checkout_time = row[4].strip()

                # Match name and date, and ensure no checkout time
                if log_name == name.strip().lower() and log_date == today and checkout_time == "":
                    return True

    return False


def get_registered_children(parent_name):
    children = []
    if not os.path.exists(REG_CSV): return children
    with open(REG_CSV, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 10: continue
            if row[9].lower() == parent_name.lower() and row[5] == "Child":
                children.append(f"{row[0]} {row[1]}")
    return children

def calculate_age(birth_date):
    today = datetime.today().date()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))

def get_minor_children(parent_name):
    children = []
    if not os.path.exists(REG_CSV):
        return children

    parent_name = parent_name.strip().lower()

    with open(REG_CSV, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) > 6:
                role = row[5].strip().lower()
                full_name = f"{row[0].strip()} {row[1].strip()}".lower()

                # CASE 1: If parent row contains children in the children column
                if role == "parent" and full_name == parent_name and row[6].strip():
                    # The form asks for first names only, so add the parent's
                    # surname to single names ("Ada" -> "Ada Adeyemi") to match
                    # the child's own registration and QR code.
                    parent_last = row[1].strip()
                    for c in row[6].split(","):
                        c = c.strip()
                        if not c:
                            continue
                        children.append(c if " " in c else normalize_name(f"{c} {parent_last}"))
                    return children  # Return immediately if found

    return children


def is_minor(full_name):
    if not os.path.exists(REG_CSV): return False
    with open(REG_CSV, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 9: continue
            if f"{row[0]} {row[1]}".lower() == full_name.lower():
                return row[8] == "1"  # Just use the minor flag
    return False

def get_checked_in_names():
    """Get names that are checked in but not checked out"""
    names = []
    if not os.path.exists(LOG_CSV): return names
    with open(LOG_CSV, newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 5 and not row[4]:  # No checkout time
                names.append(row[0].lower())
    return names

def email_exists(email):
    if not os.path.exists(REG_CSV): return False
    with open(REG_CSV, newline="") as f:
        return any(len(row) > 2 and row[2].lower() == email.lower() 
                  for row in csv.reader(f))

def phone_exists(phone):
    if not os.path.exists(REG_CSV): return False
    with open(REG_CSV, newline="") as f:
        return any(len(row) > 3 and row[3] == phone for row in csv.reader(f))

def parent_exists(full_name):
    if not os.path.exists(REG_CSV): return False
    with open(REG_CSV, newline="") as f:
        for row in csv.reader(f):
            if len(row) > 5 and f"{row[0]} {row[1]}".lower() == full_name.lower():
                return row[5] in ["Parent", "Adult"]
    return False

# --------------------- ROUTES ---------------------
@app.route("/")
def index():
    return redirect("/register")

@app.route("/register", methods=["GET", "POST"])
def register():
    registered_parents = get_registered_parents()

    def reg_error(message):
        """Show the form again with the person's answers kept and the problem explained."""
        return render_template(
            "register.html",
            registered_parents=registered_parents,
            errors=[message],
            form=request.form,
        ), 400

    if request.method == "POST":
        first = normalize_name(request.form["first_name"])
        last = normalize_name(request.form["last_name"])
        email = request.form["email"].strip().lower()
        phone = request.form["phone"].strip()
        gender = request.form.get("gender", "Other")
        role = request.form["role"]
        children_raw = request.form.get("children", "").strip()
        # NEW: Date of Birth (from the <input type="date" name="date_of_birth">)
        date_of_birth = (request.form.get("date_of_birth") or "").strip()
        full_name = f"{first} {last}"

        # ✅ Address only for Adults (optional)
        address = ""
        if role == "Adult":
            address = request.form.get("address", "").strip()

        # Validate names
        if name_has_number(first) or name_has_number(last):
            return reg_error("Names cannot contain numbers.")
        if '|' in first or '|' in last:
            return reg_error("Names cannot contain the '|' character.")
        if already_registered(full_name):
            return reg_error("This person is already registered.")

        # Role-specific validation
        parent_name = ""  # default
        if role == "Child":
            if children_raw:
                return reg_error("Children cannot register children under their name.")

            parent_names = request.form.getlist("parent")  # up to 2 parents
            if not parent_names:
                return reg_error("Parent/Guardian is required for children.")
            if len(parent_names) > 2:
                return reg_error("You can only select up to 2 parents.")

            # Validate each selected parent exists
            for pname in parent_names:
                if not any(pname.lower() == p.lower() for p in registered_parents):
                    return reg_error(f"Parent '{pname}' is not registered.")

            parent_name = ", ".join(parent_names)  # store both parents as CSV

        elif role == "Parent":
            parent_name = full_name  # parent self-references

        # Validate email and phone
        if email_exists(email):
            return reg_error("This email is already registered.")
        if phone_exists(phone):
            return reg_error("This phone number is already registered.")

        # Process children list (for Parent role)
        child_list = []
        if children_raw and role == "Parent":
            raw_names = [normalize_name(c) for c in children_raw.split(",") if c.strip()]
            for c in raw_names:
                if name_has_number(c):
                    return reg_error(f"Child name '{c}' contains a number.")
                if '|' in c:
                    return reg_error(f"Child name '{c}' contains invalid character '|'")
                # "Last name will be added automatically" — the form promised it, now it does
                child_list.append(c if " " in c else f"{c} {last}")

        # Generate QR code
        qr_data = f"{first}|{last}|{role}"
        base_url = request.host_url.rstrip('/')
        qr_url = f"{base_url}/check-in?data={urllib.parse.quote(qr_data)}"
        qr_filename = f"{first}_{last}.png"
        qr_path = QR_FOLDER / qr_filename
        qrcode.make(qr_url).save(str(qr_path))

        # Ensure CSV header exists and includes "Date of Birth" as LAST column
        if os.path.exists(REG_CSV):
            with open(REG_CSV, newline="") as f:
                rows = list(csv.reader(f))
            if rows:
                header = rows[0]
                if header and header[0] == "First Name" and "Date of Birth" not in header:
                    header.append("Date of Birth")
                    # pad existing rows
                    for i in range(1, len(rows)):
                        rows[i].append("")
                    with open(REG_CSV, "w", newline="") as f:
                        csv.writer(f).writerows(rows)
        else:
            with open(REG_CSV, "w", newline="") as f:
                csv.writer(f).writerow([
                    "First Name","Last Name","Email","Phone","Gender",
                    "Role","Children","QR Link","Minor","Parent Name","Address",
                    "Date of Birth"  # NEW last column
                ])

        # Write to CSV (append Date of Birth as last field)
        with open(REG_CSV, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                first,
                last,
                email,
                phone,
                gender,
                role,
                ", ".join(child_list),  # children
                qr_url,
                "1" if role == "Child" else "0",
                parent_name,
                address,
                date_of_birth  # NEW last column
            ])

        # Send QR (best-effort)
        if email:
            try:
                send_qr_email(email, full_name, str(qr_path))
            except Exception as e:
                print(f"Error sending email: {e}")

        return render_template("qrcode.html", name=full_name, qr_url=f"/static/qrcodes/{qr_filename}")

    return render_template("register.html", registered_parents=registered_parents)

# app.py
@app.route("/scan")
def scan():
    return render_template("scan.html")


@app.route("/check-in", methods=["GET", "POST"])
def check_in():
    # --- robust QR parsing ---
    raw = request.args.get("data", "") or ""
    qr_data = urllib.parse.unquote(raw)

    # If scanner handed us a full URL (e.g., Google/Lens wrapper), peel ?data=...
    if qr_data.startswith("http"):
        try:
            u = urllib.parse.urlparse(qr_data)
            qs = urllib.parse.parse_qs(u.query)
            if "data" in qs and qs["data"]:
                qr_data = qs["data"][0]
        except Exception:
            pass

    # If it still contains 'data=' without pipes, strip it and decode again
    if "data=" in qr_data and "|" not in qr_data:
        qr_data = qr_data.split("data=", 1)[1]
    qr_data = urllib.parse.unquote(qr_data)

    if not qr_data:
        return notice("That code didn't scan", "No check-in details were found in the QR code. Try scanning it again, holding it steady inside the frame.")

    # Parse parts
    try:
        parts = qr_data.split("|")
        if len(parts) < 3:
            return notice("This isn't a check-in code", "The QR code doesn't contain a name and role. Ask the welcome desk to register you and print a new code.")
        first = parts[0].strip()
        last  = parts[1].strip()
        role  = parts[2].split(",")[0].strip()
        name  = normalize_name(f"{first} {last}")
    except Exception:
        return notice("That code couldn't be read", "Something in the QR code is damaged. Ask the welcome desk to regenerate it.")

    # If already checked in, bounce to checkout using the CLEANED qr_data
    if is_checked_in(name):
        return redirect(url_for("check_out", data=urllib.parse.quote(qr_data)))

    # Handle children if parent
    minor_children = get_minor_children(name) if role.lower() == "parent" else []
    unscanned_minors = [c for c in minor_children if not is_checked_in(c)]

    # GET → show form
    if request.method == "GET":
        return render_template(
            "check_in.html",
            name=name,
            role=role,
            unscanned_children=unscanned_minors,
            has_children_registered=len(minor_children) > 0,
            is_checked_in=False
        )

    # POST → process check-in
    timestamp   = datetime.now()
    date_str    = str(timestamp.date())
    time_str    = timestamp.strftime("%H:%M:%S")
    checkin_by  = "QR"
    selected_children = []

    # Ensure log header
    if not os.path.exists(LOG_CSV):
        with open(LOG_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Name", "Role", "Date", "CheckIn", "CheckOut", "Method", "Parent"])

    with open(LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)

        if role.lower() == "parent":
            selected_children = request.form.getlist("children")
            no_kids = "no_kids" in request.form
            # Parent row
            writer.writerow([name, "Parent", date_str, time_str, "", checkin_by, name])
            # Children rows
            if not no_kids:
                for child in selected_children:
                    if not is_checked_in(child):
                        writer.writerow([child, "Child", date_str, time_str, "", checkin_by, name])

        elif role.lower() == "child":
            parent_name = ""
            if os.path.exists(REG_CSV):
                with open(REG_CSV, newline="") as reg_file:
                    for row in csv.reader(reg_file):
                        if len(row) > 9 and f"{row[0]} {row[1]}".strip().lower() == name.lower():
                            parent_name = row[9].strip() if row[9] else ""
                            break
            writer.writerow([name, "Child", date_str, time_str, "", checkin_by, parent_name])

        else:
            writer.writerow([name, "Adult", date_str, time_str, "", checkin_by, ""])

    # Success page → auto-returns to /scan
    return render_template(
        "checkin_success.html",
        name=name,
        time=time_str,
        children=selected_children
    )

@app.route("/check-out", methods=["GET", "POST"])
def check_out():
    # --- robust QR parsing (handles Google/Lens wrappers, double-encoding, stray data=) ---
    raw = request.args.get("data", "") or ""
    qr_data = urllib.parse.unquote(raw)

    if qr_data.startswith("http"):
        try:
            u = urllib.parse.urlparse(qr_data)
            qs = urllib.parse.parse_qs(u.query)
            if "data" in qs and qs["data"]:
                qr_data = qs["data"][0]
        except Exception:
            pass

    if "data=" in qr_data and "|" not in qr_data:
        qr_data = qr_data.split("data=", 1)[1]
    qr_data = urllib.parse.unquote(qr_data)

    if not qr_data:
        return notice("That code didn't scan", "No check-in details were found in the QR code. Try scanning it again.")

    # Parse parts
    try:
        parts = qr_data.split("|")
        if len(parts) < 3:
            return notice("This isn't a check-in code", "The QR code doesn't contain a name and role.")
        first = parts[0].strip()
        last  = parts[1].strip()
        role  = parts[2].split(",")[0].strip()
        name  = normalize_name(f"{first} {last}")
    except Exception:
        return notice("That code couldn't be read", "Something in the QR code is damaged. Ask the welcome desk to regenerate it.")

    # Build checkout list (only include members who are actually checked in)
    checkout_list = []
    if is_checked_in(name):
        checkout_list.append(name)

    if role.lower() == "parent":
        children = get_minor_children(name)
        for c in children:
            if is_checked_in(c):
                checkout_list.append(c)

    # If nobody is actually checked in, tell user immediately
    if request.method == "GET":
        if not checkout_list:
            return notice("Nobody to check out", f"{name} and their family aren't checked in today.", kind="info", status=200)
        return render_template("check_out.html", name=name, checkout_list=checkout_list)

    # POST → process checkout
    selected_members = [normalize_name(m) for m in request.form.getlist("members")]
    if not selected_members:
        # fallback: if user didn’t tick anything, try to check out the scanned person
        selected_members = [name]

    if not os.path.exists(LOG_CSV):
        return notice("Nobody to check out", "There are no check-ins recorded today.", kind="info", status=200)

    today = str(datetime.now().date())
    timestamp = datetime.now().strftime("%H:%M:%S")

    # Read logs and preserve header
    with open(LOG_CSV, newline="") as f:
        rows = list(csv.reader(f))

    header = []
    data = rows
    if rows and rows[0] and rows[0][0] == "Name":
        header, data = rows[0], rows[1:]

    found_any = False
    for row in data:
        if len(row) < 5:
            continue
        log_name = normalize_name(row[0])
        log_date = (row[2] or "").strip()
        checkout_time = (row[4] or "").strip()

        if (log_name in selected_members) and (log_date == today) and (checkout_time == ""):
            row[4] = timestamp
            found_any = True

    if not found_any:
        return notice("Nobody to check out", "None of the selected people are checked in today.", kind="info", status=200)

    # Write back with header intact
    out = [header] + data if header else data
    with open(LOG_CSV, "w", newline="") as f:
        csv.writer(f).writerows(out)

    # Success page (optionally show who got checked out)
    checked_children = [m for m in selected_members if m != name]
    return render_template("checkout_success.html", name=name, children=checked_children)




@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():

    # Load logs, remembering each row's position in the file so the
    # delete buttons remove the right entry (the header is row 0).
    logs = []
    if os.path.exists(LOG_CSV):
        with open(LOG_CSV, newline="") as f:
            raw_logs = list(csv.reader(f))
        for i, row in enumerate(raw_logs):
            if not row or row[0] == "Name":
                continue
            row = row + [""] * (7 - len(row))
            logs.append({"index": i, "row": row})

    # Load registrations and process family info (now includes DOB)
    registrations = []
    if os.path.exists(REG_CSV):
        with open(REG_CSV, newline="") as f:
            reader = csv.reader(f)
            for raw_index, row in enumerate(reader):
                # Skip header if present
                if not row or row[0].strip() == "First Name":
                    continue

                # Ensure we have up to DOB column (index 11)
                if len(row) < 12:
                    row += [""] * (12 - len(row))

                first       = (row[0] or "").strip()
                last        = (row[1] or "").strip()
                email       = (row[2] or "").strip()
                phone       = (row[3] or "").strip()
                role        = (row[5] or "").strip()
                children    = (row[6] or "").strip()
                parents_raw = (row[9] or "").strip()
                address     = (row[10] or "").strip() if role == "Adult" else ""
                dob         = (row[11] or "").strip()  # ← NEW

                parents = ", ".join([p.strip() for p in parents_raw.split(",") if p.strip()])

                registrations.append({
                    "index": raw_index,
                    "name": f"{first} {last}",
                    "email": email,
                    "phone": phone,
                    "role": role,
                    "children": children,
                    "parents": parents,
                    "address": address,
                    "date_of_birth": dob,  # ← pass to template
                })

    # ✅ Notifications
    all_checked_out = session.pop("all_checked_out", False)
    logs_cleared = session.pop("logs_cleared", False)

    today = str(datetime.now().date())
    todays_logs = [l["row"] for l in logs if l["row"][2] == today]
    stats = {
        "here_now": sum(1 for r in todays_logs if not r[4]),
        "children_here": sum(1 for r in todays_logs if not r[4] and r[1] == "Child"),
        "checked_in_today": len(todays_logs),
        "registered": len(registrations),
    }

    return render_template(
        "dashboard.html",
        logs=logs,
        registrations=registrations,
        stats=stats,
        all_checked_out=all_checked_out,
        logs_cleared=logs_cleared
    )



@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")

@app.route("/api/logs")
@login_required
def api_logs():
    if not os.path.exists(LOG_CSV): return jsonify([])
    with open(LOG_CSV, newline="") as f:
        return jsonify(list(csv.reader(f)))

@app.route("/manual-checkin", methods=["POST"])
def manual_checkin():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401
    
    name = request.args.get("name")
    if not name: return "❌ No name provided."
    
    # Prevent CSV/formula injection without mangling names like "Mary-Jane":
    # drop control characters and commas, then any leading formula characters.
    for char in [',', ';', '\t', '\r', '\n']:
        name = name.replace(char, '')
    name = normalize_name(name.lstrip("=+-@ "))
    
    if is_checked_in(name):
        return f"❌ {name} is already checked in."
    
    # Look up role
    role = "Adult"
    if os.path.exists(REG_CSV):
        with open(REG_CSV, newline="") as f:
            for row in csv.reader(f):
                if len(row) > 5 and f"{row[0]} {row[1]}" == name:
                    role = row[5]
                    break
    
    timestamp = datetime.now()
    new_file = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["Name", "Role", "Date", "CheckIn", "CheckOut", "Method", "Parent"])
        # Empty checkout column; same 7 columns as QR check-ins
        writer.writerow([name, role, str(timestamp.date()),
                         timestamp.strftime("%H:%M:%S"), "", "Admin", ""])

    return f"✅ {name} checked in."

# Add this new route to app.py
@app.route("/manual-checkout", methods=["POST"])
def manual_checkout():
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401
    
    name = request.args.get("name")
    if not name: 
        return "❌ No name provided."
    
    # Find the open check-in record
    if not os.path.exists(LOG_CSV):
        return "❌ No check-in records found."
    
    # Read all logs
    with open(LOG_CSV, "r") as f:
        logs = list(csv.reader(f))
    
    # Find the header row
    header = None
    if logs and logs[0][0] == "Name":
        header = logs[0]
        logs = logs[1:]
    
    found = False
    for i, row in enumerate(logs):
        # Ensure we have enough columns
        if len(row) < 5:
            continue
            
        # Clean and compare names
        stored_name = row[0].strip()
        requested_name = name.strip()
        
        # Check if this is the record we want to check out
        if stored_name == requested_name and not row[4].strip():
            # Set checkout time
            checkout_time = datetime.now().strftime("%H:%M:%S")
            logs[i][4] = checkout_time
            found = True
            break
    
    if found:
        # Write updated logs back to file
        with open(LOG_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            if header:
                writer.writerow(header)
            writer.writerows(logs)
        return f"✅ {name} checked out."
    else:
        return f"❌ {name} isn't checked in, so there's nothing to check out."


@app.route("/search-registrations")
@login_required
def search_registrations():
    query = request.args.get("query", "").lower().strip()
    results = []
    
    if query and os.path.exists(REG_CSV):
        with open(REG_CSV, newline="") as f:
            for row in csv.reader(f):
                if len(row) < 4: continue
                name = f"{row[0]} {row[1]}".lower()
                email = row[2].lower() if len(row) > 2 else ""
                phone = row[3] if len(row) > 3 else ""
                
                if (query in name or query in email or query in phone):
                    results.append({
                        "name": f"{row[0]} {row[1]}",
                        "email": email,
                        "phone": phone
                    })
                
    return jsonify(results)

@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pin = request.form.get("pin") or ""
        if secrets.compare_digest(pin, ADMIN_PIN):
            session.permanent = True
            session["authenticated"] = True
            return redirect("/dashboard")
        else:
            return "❌ Incorrect PIN"
    return render_template("admin_login.html")

@app.route("/delete-log/<int:index>", methods=["POST"])
@login_required
def delete_log(index):
    if not os.path.exists(LOG_CSV): return redirect("/dashboard")
    
    with open(LOG_CSV, newline="") as f:
        logs = list(csv.reader(f))
    # index is the row's position in the file; never delete the header row
    is_header = 0 <= index < len(logs) and logs[index] and logs[index][0] == "Name"
    if 0 <= index < len(logs) and not is_header:
        logs.pop(index)
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerows(logs)
    return redirect("/dashboard")

@app.route("/admin-registrations")
@login_required
def admin_registrations():
    registrations = []
    if os.path.exists(REG_CSV):
        with open(REG_CSV, newline="") as f:
            for i, row in enumerate(csv.reader(f)):
                if not row or row[0].strip() == "First Name":
                    continue  # header row is not a person
                row = row + [""] * (12 - len(row))
                registrations.append({"index": i, "row": row})
    return render_template("admin_registrations.html", registrations=registrations)

@app.route("/delete-registration/<int:index>", methods=["POST"])
@login_required
def delete_registration(index):
    if not os.path.exists(REG_CSV): return redirect("/admin-registrations")
    
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))
    is_header = (0 <= index < len(registrations) and registrations[index]
                 and registrations[index][0].strip() == "First Name")
    if 0 <= index < len(registrations) and not is_header:
        registrations.pop(index)
        with open(REG_CSV, "w", newline="") as f:
            csv.writer(f).writerows(registrations)
    return redirect(request.referrer or "/admin-registrations")

@app.route("/edit-registration/<int:index>", methods=["GET", "POST"])
@login_required
def edit_registration(index):
    if not os.path.exists(REG_CSV):
        return "No registrations"

    # Load and upgrade CSV to include "Date of Birth" as last header if missing
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))

    if not registrations:
        return "No registrations"

    # If header present and missing DOB, append it and pad all rows
    header = registrations[0]
    has_header = header and header[0].strip() == "First Name"
    if has_header and "Date of Birth" not in header:
        header.append("Date of Birth")
        for i in range(1, len(registrations)):
            registrations[i].append("")
        with open(REG_CSV, "w", newline="") as f:
            csv.writer(f).writerows(registrations)

    # Re-read after potential upgrade so indexing stays consistent
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))

    if index < 0 or index >= len(registrations):
        return "Invalid registration index"

    reg = registrations[index]

    # Prevent editing the header row if your UI ever passes index 0
    if reg and reg[0].strip() == "First Name":
        return "Invalid registration index"

    # Ensure at least 12 columns (DOB is index 11)
    if len(reg) < 12:
        reg += [""] * (12 - len(reg))

    if request.method == "POST":
        first = normalize_name(request.form["first_name"])
        last = normalize_name(request.form["last_name"])
        email = request.form["email"].strip().lower()
        phone = request.form["phone"].strip()
        gender = request.form["gender"]
        role = request.form["role"]
        children_raw = request.form.get("children", "").strip()
        regenerate_qr = request.form.get("regenerate_qr", "0") == "1"

        # Address: only keep for Adults (blank otherwise)
        address = ""
        if role == "Adult":
            address = request.form.get("address", "").strip()

        # NEW: Date of Birth (YYYY-MM-DD from <input type="date">)
        date_of_birth = (request.form.get("date_of_birth") or "").strip()

        # Minor flag based on role (keep your existing logic)
        minor_flag = "1" if role == "Child" else "0"

        # Parent name handling (keep existing unless you expose editing)
        parent_name = reg[9] if len(reg) > 9 else ""
        if role == "Parent":
            parent_name = f"{first} {last}"
        elif role == "Child":
            chosen = [p for p in request.form.getlist("parent") if p.strip()][:2]
            parent_name = ", ".join(chosen) or request.form.get("parent_name") or parent_name

        # Keep existing QR unless regenerating
        qr_url = reg[7] if len(reg) > 7 else ""

        # Regenerate QR if requested (FIXED format to first|last|role)
        if regenerate_qr:
            qr_data = f"{first}|{last}|{role}"
            base_url = request.host_url.rstrip('/')
            qr_url = f"{base_url}/check-in?data={urllib.parse.quote(qr_data)}"
            qr_filename = f"{first}_{last}.png"
            qr_path = QR_FOLDER / qr_filename
            qrcode.make(qr_url).save(str(qr_path))
            if email:
                try:
                    send_qr_email(email, f"{first} {last}", str(qr_path))
                except Exception as e:
                    print(f"Error sending email: {e}")

        # Write back the updated row (DOB last at index 11)
        new_reg = [
            first,            # 0 First Name
            last,             # 1 Last Name
            email,            # 2 Email
            phone,            # 3 Phone
            gender,           # 4 Gender
            role,             # 5 Role
            children_raw,     # 6 Children
            qr_url,           # 7 QR Link
            minor_flag,       # 8 Minor
            parent_name,      # 9 Parent Name
            address,          # 10 Address
            date_of_birth     # 11 Date of Birth (NEW)
        ]

        registrations[index] = new_reg
        with open(REG_CSV, "w", newline="") as f:
            csv.writer(f).writerows(registrations)
        return redirect("/admin-registrations")

    # GET: render the edit page (you pass the list to the template)
    # Ensure reg has DOB slot for the template
    if len(reg) < 12:
        reg += [""] * (12 - len(reg))

    full_name = f"{reg[0]} {reg[1]}"
    is_checked_in_flag = is_checked_in(full_name)
    return render_template(
        "edit_registration.html",
        reg=reg,
        index=index,
        registered_parents=get_registered_parents(),
        is_checked_in=is_checked_in_flag
    )

@app.route("/resend-qr/<int:index>")
@login_required
def resend_qr(index):
    back = dict(action_href="/admin-registrations", action_label="Back to registrations")
    if not os.path.exists(REG_CSV):
        return notice("No registrations yet", "Register someone first.", **back)

    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))
    if index < 0 or index >= len(registrations) or registrations[index][0] == "First Name":
        return notice("Registration not found", "It may have been deleted. Refresh the list and try again.", **back)

    reg = registrations[index]
    name = f"{reg[0]} {reg[1]}"
    # QR images are saved as First_Last.png when someone registers
    qr_path = QR_FOLDER / f"{reg[0]}_{reg[1]}.png"
    if not qr_path.exists():
        return notice("No QR image on file", f"Open {name}'s registration and choose Regenerate QR to make a new one.", **back)

    if len(reg) < 3 or not reg[2]:
        return notice("No email address", f"Add an email address to {name}'s registration, then resend.", **back)

    if send_qr_email(reg[2], name, str(qr_path)):
        return notice("QR code sent", f"{name}'s QR code was emailed to {reg[2]}.", kind="success", status=200, **back)
    return notice("Email couldn't be sent", "Email isn't set up yet, or the mail server rejected it. Set EMAIL_HOST, EMAIL_USER and EMAIL_PASS, then try again.", **back)

@app.route("/check-out/<int:index>", methods=["POST"])
@login_required
def admin_check_out(index):
    
    # Get the registration
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))
    if index < 0 or index >= len(registrations):
        return "Invalid registration index"
    
    reg = registrations[index]
    full_name = f"{reg[0]} {reg[1]}"
    
    # Perform checkout
    timestamp = datetime.now()
    date_str = str(timestamp.date())
    time_str = timestamp.strftime("%H:%M:%S")
    
    # Find the open check-in record
    logs = []
    if os.path.exists(LOG_CSV):
        with open(LOG_CSV, newline="") as f:
            logs = list(csv.reader(f))
    
    found = False
    for i, row in enumerate(logs):
        if len(row) >= 5 and row[0] == full_name and not row[4]:
            logs[i][4] = time_str  # Set checkout time
            found = True
            break
    
    if found:
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerows(logs)
        return redirect(f"/edit-registration/{index}")
    return notice("Nobody to check out", f"{full_name} isn't checked in.", kind="info", status=200,
                  action_href=f"/edit-registration/{index}", action_label="Back to registration")


# Add this to app.py (run once to update existing registrations)
@app.route("/update-qr-codes")
@login_required
def update_qr_codes():
    if not os.path.exists(REG_CSV): 
        return "No registrations"
    
    updated = 0
    registrations = []
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))
    
    base_url = request.host_url.rstrip('/')
    
    for i, reg in enumerate(registrations):
        if len(reg) < 8 or not reg[7] or reg[0] == "First Name":
            continue  # skip the header row and rows without a QR

        # First and last names live in their own columns
        first = reg[0].strip()
        last = reg[1].strip()
        role = reg[5]
        
        # FIX: Generate new QR format (first|last|role)
        role_clean = role.split(',')[0]  # Remove any extra parameters
        qr_data = urllib.parse.quote(f"{first}|{last}|{role_clean}")
        qr_url = f"{base_url}/check-in?data={qr_data}"
        
        # Update registration
        registrations[i][7] = qr_url
        
        # Regenerate QR image
        qr_filename = f"{first}_{last}.png"
        qr_path = QR_FOLDER / qr_filename
        qrcode.make(qr_url).save(str(qr_path))
        updated += 1

    # Save updated registrations
    with open(REG_CSV, "w", newline="") as f:
        csv.writer(f).writerows(registrations)
        
    return f"Updated {updated} QR codes"

@app.route("/download-logs")
@login_required
def download_logs():

    if not os.path.exists(LOG_CSV):
        return notice("No logs to download", "Nobody has checked in yet.", kind="info", status=200,
                      action_href="/dashboard", action_label="Back to dashboard")

    # Downloading only clears the log when it comes from the confirm page
    # (/download-logs?clear=1). A plain download never deletes anything.
    clear_after = request.args.get("clear") == "1"

    date_str = datetime.now().strftime("%d-%m-%Y")
    download_name = f"check-in-logs-{date_str}.csv"

    response = send_from_directory(
        directory=os.path.dirname(LOG_CSV),
        path=os.path.basename(LOG_CSV),
        as_attachment=True,
        download_name=download_name
    )

    if clear_after:
        @response.call_on_close
        def clear_logs():
            # Reset the log file and keep only the header row
            with open(LOG_CSV, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Name", "Role", "Date", "CheckIn", "CheckOut", "Method", "Parent"])

    return response



@app.route("/confirm-clear-logs")
@login_required
def confirm_clear_logs():
    return render_template("confirm_clear_logs.html")


@app.route("/clear-logs", methods=["POST"])
@login_required
def clear_logs():

    # Recreate the logs CSV with just the header
    with open(LOG_CSV, "w", newline="") as f:
        csv.writer(f).writerow(
            ["Name","Role","Date","CheckIn","CheckOut","Method","Parent"]
        )

    session["logs_cleared"] = True
    return redirect("/dashboard")



if __name__ == "__main__":
    import socket, threading, webbrowser, time

    def get_local_ip():
        """Return the LAN IP (e.g., 192.168.x.x)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Uses routing table to pick the active interface; no traffic is sent.
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            try:
                ip = socket.gethostbyname(socket.gethostname())
            except Exception:
                ip = "127.0.0.1"  # fall back to this machine only
        finally:
            s.close()
        return ip

    def open_browser():
        ip = get_local_ip()
        # Small delay so Flask is listening before we open the page
        time.sleep(1.0)
        webbrowser.open(f"http://{ip}:5000")

    if ADMIN_PIN == "4321":
        print("⚠️  Using the default admin PIN 4321. Set ADMIN_PIN to change it.")

    threading.Thread(target=open_browser, daemon=True).start()
    # Serve on all interfaces so other devices on Wi-Fi can reach it.
    # Debug mode exposes an interactive console to anyone on the network,
    # so it is off unless FLASK_DEBUG=1 is set explicitly.
    debug = os.getenv("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=debug)

