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
RECENT_CHECKINS = defaultdict(float)
RESCAN_COOLDOWN_SECONDS = 8


app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your_secure_secret_key')
ADMIN_PIN = os.getenv('ADMIN_PIN', '4321')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

if getattr(sys, 'frozen', False):
    app.template_folder = os.path.join(sys._MEIPASS, 'templates')
    app.static_folder = os.path.join(sys._MEIPASS, 'static')

# Email configuration
EMAIL_HOST = ""
EMAIL_PORT = 587
EMAIL_USER = ""
EMAIL_PASS = ""

def send_qr_email(recipient_email, name, qr_path):
    """Send a beautiful HTML email with QR code attachment"""
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = EMAIL_USER
        msg["To"] = recipient_email
        msg["Subject"] = "Welcome to Watford Church – Your QR Code"

        # HTML email content
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <h2 style="color:#2E86C1;">Hi {name},</h2>
            <p>Welcome to <strong>ENTER BUSINESS HERE</strong>! 🎉</p>
            <p>We’re so excited to have you join our community.</p>
            <p>Your QR code for check-in is attached to this email.</p>
            <p>Please bring this QR code with you upon entrance for fast and easy check-in.</p>
            <p style="margin-top: 20px;">THANK YOU,<br><strong>BUSINESS NAME</strong></p>
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

        print(f"✅ Beautiful HTML email sent to {recipient_email}")

    except Exception as e:
        print(f"❌ Error sending email to {recipient_email}: {e}")


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
    return " ".join(part.capitalize() for part in name.strip().split())

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
                    children = [c.strip() for c in row[6].split(",") if c.strip()]
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
            return "❌ Names cannot contain numbers."
        if '|' in first or '|' in last:
            return "❌ Names cannot contain the '|' character."
        if already_registered(full_name):
            return "❌ This person is already registered."

        # Role-specific validation
        parent_name = ""  # default
        if role == "Child":
            if children_raw:
                return "❌ Children cannot register children under their name."

            parent_names = request.form.getlist("parent")  # up to 2 parents
            if not parent_names:
                return "❌ Parent/Guardian is required for children."
            if len(parent_names) > 2:
                return "❌ You can only select up to 2 parents."

            # Validate each selected parent exists
            for pname in parent_names:
                if not any(pname.lower() == p.lower() for p in registered_parents):
                    return f"❌ Parent '{pname}' is not registered."

            parent_name = ", ".join(parent_names)  # store both parents as CSV

        elif role == "Parent":
            parent_name = full_name  # parent self-references

        # Validate email and phone
        if email_exists(email):
            return "❌ This email is already registered."
        if phone_exists(phone):
            return "❌ This phone number is already registered."

        # Process children list (for Parent role)
        child_list = []
        if children_raw and role == "Parent":
            raw_names = [normalize_name(c) for c in children_raw.split(",")]
            for c in raw_names:
                if name_has_number(c):
                    return f"❌ Child name '{c}' contains a number."
                if '|' in c:
                    return f"❌ Child name '{c}' contains invalid character '|'"
                child_list.append(c)

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
        return "❌ Invalid QR scan."

    # Parse parts
    try:
        parts = qr_data.split("|")
        if len(parts) < 3:
            return "❌ Malformed QR code. Expected first|last|role format."
        first = parts[0].strip()
        last  = parts[1].strip()
        role  = parts[2].split(",")[0].strip()
        name  = normalize_name(f"{first} {last}")
    except Exception as e:
        return f"❌ Error parsing QR code: {str(e)}"

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
        return "❌ Invalid QR scan."

    # Parse parts
    try:
        parts = qr_data.split("|")
        if len(parts) < 3:
            return "❌ Malformed QR code."
        first = parts[0].strip()
        last  = parts[1].strip()
        role  = parts[2].split(",")[0].strip()
        name  = normalize_name(f"{first} {last}")
    except Exception as e:
        return f"❌ Error parsing QR code: {str(e)}"

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
            return "❌ No active check-in found for this family today."
        return render_template("check_out.html", name=name, checkout_list=checkout_list)

    # POST → process checkout
    selected_members = [normalize_name(m) for m in request.form.getlist("members")]
    if not selected_members:
        # fallback: if user didn’t tick anything, try to check out the scanned person
        selected_members = [name]

    if not os.path.exists(LOG_CSV):
        return "❌ No active check-in found."

    from datetime import datetime
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
        return "❌ No active check-in found."

    # Write back with header intact
    out = [header] + data if header else data
    with open(LOG_CSV, "w", newline="") as f:
        csv.writer(f).writerows(out)

    # Success page (optionally show who got checked out)
    checked_children = [m for m in selected_members if m != name]
    return render_template("checkout_success.html", name=name, children=checked_children)




@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if not session.get("authenticated"):
        return redirect("/admin-login")

    # Load logs
    logs = []
    if os.path.exists(LOG_CSV):
        with open(LOG_CSV, newline="") as f:
            logs = list(csv.reader(f))
        if logs and logs[0][0] == "Name":  # Skip header row
            logs = logs[1:]

    # Load registrations and process family info (now includes DOB)
    registrations = []
    if os.path.exists(REG_CSV):
        with open(REG_CSV, newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                # Skip header if present
                if row and row[0].strip() == "First Name":
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

    return render_template(
        "dashboard.html",
        logs=logs,
        registrations=registrations,
        all_checked_out=all_checked_out,
        logs_cleared=logs_cleared
    )



@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")

@app.route("/api/logs")
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
    
    # Sanitize name to prevent CSV injection
    dangerous_chars = [',', ';', '=', '+', '-', '@', '\t', '\r', '\n']
    for char in dangerous_chars:
        name = name.replace(char, '')
    name = normalize_name(name)
    
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
    with open(LOG_CSV, "a", newline="") as f:
        # Add empty checkout column
        csv.writer(f).writerow([name, role, str(timestamp.date()), 
                               timestamp.strftime("%H:%M:%S"), "", "Admin"])
    
    return f"✅ {name} manually checked in."

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
        return f"✅ {name} checked out successfully."
    else:
        # Diagnostic information
        active_found = any(row[0].strip() == name.strip() and not row[4].strip() for row in logs)
        return f"❌ No active check-in found for {name}. Active check-in exists: {active_found}"   


@app.route("/search-registrations")
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
        pin = request.form.get("pin")
        if pin == ADMIN_PIN:
            session["authenticated"] = True
            return redirect("/dashboard")
        else:
            return "❌ Incorrect PIN"
    return render_template("admin_login.html")

@app.route("/delete-log/<int:index>", methods=["POST"])
def delete_log(index):
    if not session.get("authenticated"): return redirect("/admin-login")
    if not os.path.exists(LOG_CSV): return redirect("/dashboard")
    
    with open(LOG_CSV, newline="") as f:
        logs = list(csv.reader(f))
    if 0 <= index < len(logs):
        logs.pop(index)
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerows(logs)
    return redirect("/dashboard")

@app.route("/admin-registrations")
def admin_registrations():
    if not session.get("authenticated"): return redirect("/admin-login")
    registrations = []
    if os.path.exists(REG_CSV):
        with open(REG_CSV, newline="") as f:
            registrations = list(csv.reader(f))
    return render_template("admin_registrations.html", registrations=registrations)

@app.route("/delete-registration/<int:index>", methods=["POST"])
def delete_registration(index):
    if not session.get("authenticated"): return redirect("/admin-login")
    if not os.path.exists(REG_CSV): return redirect("/admin-registrations")
    
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))
    if 0 <= index < len(registrations):
        registrations.pop(index)
        with open(REG_CSV, "w", newline="") as f:
            csv.writer(f).writerows(registrations)
    return redirect("/admin-registrations")

@app.route("/edit-registration/<int:index>", methods=["GET", "POST"])
def edit_registration(index):
    if not session.get("authenticated"):
        return redirect("/admin-login")
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
            parent_name = request.form.get("parent_name", parent_name)

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
def resend_qr(index):
    if not session.get("authenticated"): return redirect("/admin-login")
    if not os.path.exists(REG_CSV): return "No registrations"
    
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))
    if index < 0 or index >= len(registrations):
        return "Invalid index"
    
    reg = registrations[index]
    if len(reg) < 8 or not reg[7]: return "No QR code"
    
    qr_filename = reg[7].split("/")[-1]
    qr_path = QR_FOLDER / qr_filename
    
    if len(reg) > 2 and reg[2]:
        name = f"{reg[0]} {reg[1]}"
        if send_qr_email(reg[2], name, str(qr_path)):
            return "QR code resent!"
    return "No email found"

@app.route("/check-out/<int:index>", methods=["POST"])
def admin_check_out(index):
    if not session.get("authenticated"): 
        return redirect("/admin-login")
    
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
    return "No active check-in found for this user"


# Add this to app.py (run once to update existing registrations)
@app.route("/update-qr-codes")
def update_qr_codes():
    if not os.path.exists(REG_CSV): 
        return "No registrations"
    
    updated = 0
    registrations = []
    with open(REG_CSV, newline="") as f:
        registrations = list(csv.reader(f))
    
    base_url = request.host_url.rstrip('/')
    
    for i, reg in enumerate(registrations):
        if len(reg) < 8 or not reg[7]:
            continue
        
        # Extract first/last from name
        name_parts = reg[0].split()
        first = name_parts[0]
        last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
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
def download_logs():
    if not session.get("authenticated"):
        return redirect("/admin-login")

    if not os.path.exists(LOG_CSV):
        return "❌ No logs available to download."

    date_str = datetime.now().strftime("%d-%m-%Y")
    download_name = f"check-in-logs-{date_str}.csv"

    response = send_from_directory(
        directory=os.path.dirname(LOG_CSV),
        path=os.path.basename(LOG_CSV),
        as_attachment=True,
        download_name=download_name
    )

    @response.call_on_close
    def clear_logs():
        # Reset the log file and keep only the header row
        with open(LOG_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Name", "Role", "Date", "CheckIn", "CheckOut", "Method", "Parent"])

    return response



@app.route("/confirm-clear-logs")
def confirm_clear_logs():
    if not session.get("authenticated"):
        return redirect("/admin-login")
    return render_template("confirm_clear_logs.html")


@app.route("/clear-logs", methods=["POST"])
def clear_logs():
    if not session.get("authenticated"):
        return redirect("/admin-login")

    # Recreate the logs CSV with just the header
    with open(LOG_CSV, "w", newline="") as f:
        csv.writer(f).writerow(
            ["Name","Role","Date","CheckIn","CheckOut","Method","Parent"]
        )

    session["logs_cleared"] = True
    return redirect("/dashboard")

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
                ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    def open_browser():
        ip = get_local_ip()
        # Small delay so Flask is listening before we open the page
        time.sleep(1.0)
        webbrowser.open(f"http://{ip}:5000")

    threading.Thread(target=open_browser, daemon=True).start()
    # Serve on all interfaces so other devices on Wi-Fi can reach it
    app.run(host="0.0.0.0", port=5000, debug=True)
