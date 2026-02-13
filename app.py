import secrets
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort, flash
from werkzeug.security import generate_password_hash, check_password_hash
import re
from datetime import date, datetime, timedelta
from pymongo import MongoClient, UpdateOne
from bson.objectid import ObjectId


import os
from dotenv import load_dotenv

# Load environment variables from .env file (for local development)
load_dotenv()

# -----------------------------
# FLASK INIT
# -----------------------------
app = Flask(__name__)
# Use SECRET_KEY from environment or fall back to a random one (not recommended for production persistence)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))


# -----------------------------
# MONGODB CONNECTION
# -----------------------------
# Get MongoDB URI from environment variable. 
# If not set, default to local localhost (for local development).
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(mongo_uri)
db = client.get_database("daily_ledger_db")  # Valid for both Atlas and local if URI includes db name or not

banks_col = db["banks"]
entries_col = db["daily_entries"]
shops_col = db["shops"]


# -----------------------------
# COMMON HELPERS
# -----------------------------
def current_shop_identifier():
    return session.get("shop_identifier")


def find_shop_by_identifier(identifier):
    return shops_col.find_one({
        "$or": [
            {"identifier": identifier},
            {"mobile": identifier},
            {"email": identifier}
        ]
    })


def get_shop_banks():
    return list(banks_col.find({"shop_identifier": current_shop_identifier()}))


def get_entries_in_range(start_date, end_date):
    return list(entries_col.find({
        "date": {"$gte": start_date, "$lte": end_date},
        "shop_identifier": current_shop_identifier()
    }))


def parse_entry_datetime(entry):
    d_str = entry.get("date", "1970-01-01")
    t_str = entry.get("time", "00:00:00")
    try:
        return datetime.strptime(f"{d_str} {t_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(f"{d_str} {t_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            return datetime.min


def render_add_bank_page(error=None):
    return render_template("add_bank.html", banks=get_shop_banks(), error=error)


def render_daily_entry_page(banks, today, selected_bank=None, error=None, entries=None):
    if entries is None:
        entries = []
    return render_template(
        "daily_entry.html",
        banks=banks,
        entries=entries,
        error=error,
        today=today,
        selected_bank=selected_bank
    )


# -----------------------------
# HELPER: RECALCULATE BALANCES
# -----------------------------
def recalculate_bank_balances(bank_id):
    """
    Recalculate opening & remaining balances
    using entry_datetime (correct order)
    """
    shop_identifier = current_shop_identifier()
    oid = to_object_id(bank_id)
    if not oid:
        return 
        
    bank = banks_col.find_one({"_id": oid, "shop_identifier": shop_identifier})
    if not bank:
        return

    # Ensure bank_id is String for Entry lookup (as stored in add_entry)
    str_bank_id = str(bank_id)
    
    raw_entries = list(entries_col.find({"bank_id": str_bank_id, "shop_identifier": shop_identifier}))
    entries = sorted(raw_entries, key=parse_entry_datetime)

    balance = bank["opening_balance"]
    bulk_ops = []

    for e in entries:
        # DATA CLEANING: Coerce types to ensure no string/float mismatch
        try:
            credited = float(e.get("credited", 0))
        except:
            credited = 0.0
            
        try:
            debited = float(e.get("debited", 0))
        except:
            debited = 0.0

        opening_balance = balance
        balance = balance + credited - debited
        
        # Always update entry_datetime to be correct based on date/time fields
        correct_dt = parse_entry_datetime(e)
        
        # Standardize time string to HH:MM:SS
        standard_time = correct_dt.strftime("%H:%M:%S")

        updates = {
            "opening_balance": opening_balance,
            "remaining_balance": balance,
            "credited": credited,  # Force float
            "debited": debited,    # Force float
            "entry_datetime": correct_dt,
            "time": standard_time  # Force standard format
        }

        bulk_ops.append(UpdateOne(
            {"_id": e["_id"]},
            {"$set": updates}
        ))

    if bulk_ops:
        entries_col.bulk_write(bulk_ops)


# -----------------------------
# REPORT HELPERS
# -----------------------------
def build_report(entries):
    total_credit = sum(e["credited"] for e in entries)
    total_debit = sum(e["debited"] for e in entries)

    summary = {}
    for e in entries:
        b = e["bank_name"]
        e_dt = e.get("entry_datetime") or parse_entry_datetime(e)
        summary.setdefault(b, {"credit": 0, "debit": 0, "close": e["remaining_balance"], "dt": e_dt})
        summary[b]["credit"] += e["credited"]
        summary[b]["debit"] += e["debited"]
        if e_dt >= summary[b]["dt"]:
            summary[b]["close"] = e["remaining_balance"]
            summary[b]["dt"] = e_dt

    bank_wise = []
    for b, d in summary.items():
        bank_wise.append({
            "bank": b,
            "total_credit": d["credit"],
            "total_debit": d["debit"],
            "closing_balance": d["close"]
        })
    bank_wise.sort(key=lambda x: x["bank"].lower() if isinstance(x["bank"], str) else "")

    top_banks = sorted(bank_wise, key=lambda x: x["total_credit"] + x["total_debit"], reverse=True)
    most_used = top_banks[0]["bank"] if top_banks else "N/A"
    top_bank_names = [b["bank"] for b in top_banks[:3]]

    highest_amt = 0
    highest_bank = "N/A"
    for e in entries:
        amt = max(e["credited"], e["debited"])
        if amt >= highest_amt:
            highest_amt = amt
            highest_bank = e["bank_name"]

    report = {
        "total_credit": total_credit,
        "total_debit": total_debit,
        "most_used_bank": most_used,
        "highest_amount": highest_amt,
        "highest_bank": highest_bank,
        "top_banks": top_bank_names,
        "closing_balance": sum(b["closing_balance"] for b in bank_wise)
    }

    return report, bank_wise


# -----------------------------
# CSRF HELPERS
# -----------------------------
def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


def verify_csrf():
    token = session.get("csrf_token")
    form_token = request.form.get("csrf_token")
    if not token or not form_token or token != form_token:
        abort(403)


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": get_csrf_token()}


# -----------------------------
# INPUT VALIDATION HELPERS
# -----------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MOBILE_RE = re.compile(r"^\d{10,15}$")


def is_valid_identifier(value):
    if not value:
        return False
    value = value.strip()
    return bool(EMAIL_RE.match(value) or MOBILE_RE.match(value))


def is_valid_password(value):
    if not value:
        return False
    return len(value) >= 6


def is_valid_shop_name(value):
    if not value:
        return False
    return 2 <= len(value.strip()) <= 60


def parse_non_negative_float(value):
    try:
        num = float(value)
    except Exception:
        return None
    if num < 0:
        return None
    return num


def to_object_id(value):
    try:
        return ObjectId(value)
    except Exception:
        return None


# -----------------------------
# LOGIN REQUIRED (SIMPLE GUARD)
# -----------------------------
@app.before_request
def require_login():
    allowed_paths = {"/login", "/signup"}
    if request.path in allowed_paths or request.path.startswith("/static"):
        return None
    if not session.get("shop_name"):
        return redirect(url_for("login"))


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# -----------------------------
# HOME
# -----------------------------
@app.route("/")
def home():
    return render_template("home.html")


# -----------------------------
# AUTH (SIMPLE SHOP LOGIN)
# -----------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        verify_csrf()
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""
        shop_name = (request.form.get("shop_name") or "").strip()

        if not is_valid_identifier(identifier):
            error = "Enter a valid email or mobile number"
        elif not is_valid_password(password):
            error = "Password must be at least 6 characters"
        elif not is_valid_shop_name(shop_name):
            error = "Shop name must be 2-60 characters"
        else:
            existing = find_shop_by_identifier(identifier)
            if existing:
                error = "Email or mobile already registered. Please log in."
            else:
                shops_col.insert_one({
                    "name": shop_name,
                    "identifier": identifier,
                    "password_hash": generate_password_hash(password)
                })
                return redirect(url_for("login"))

    return render_template("signup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        verify_csrf()
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""
        if not is_valid_identifier(identifier):
            error = "Enter a valid email or mobile number"
        elif not is_valid_password(password):
            error = "Password must be at least 6 characters"
        else:
            existing = find_shop_by_identifier(identifier)
            if not existing or not check_password_hash(existing.get("password_hash", ""), password):
                error = "Invalid email/mobile or password"
            else:
                session["shop_name"] = existing.get("name")
                session["shop_identifier"] = identifier
                return redirect(url_for("home"))

    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    verify_csrf()
    session.pop("shop_name", None)
    session.pop("shop_identifier", None)
    return redirect(url_for("home"))


# -----------------------------
# ADD BANK
# -----------------------------
@app.route("/add-bank", methods=["GET", "POST"])
def add_bank():
    if request.method == "POST":
        verify_csrf()
        bank_name = (request.form.get("bank_name") or "").strip()
        opening_balance = parse_non_negative_float(request.form.get("opening_balance"))
        if not bank_name or len(bank_name) > 60:
            return render_add_bank_page(error="Enter a valid bank name")
        
        # Check for duplicate bank name (case-insensitive)
        existing_bank = banks_col.find_one({
            "shop_identifier": current_shop_identifier(),
            "name": {"$regex": f"^{re.escape(bank_name)}$", "$options": "i"}
        })
        if existing_bank:
             return render_add_bank_page(error="Bank name already exists")

        if opening_balance is None:
            return render_add_bank_page(error="Opening balance must be a non-negative number")
        banks_col.insert_one({
            "name": bank_name,
            "opening_balance": opening_balance,
            "shop_identifier": current_shop_identifier()
        })
        flash(f"'{bank_name}' Bank added successfully!", 'success')
        return redirect(url_for("add_bank"))

    return render_add_bank_page()


@app.route("/edit-bank/<bank_id>", methods=["GET", "POST"])
def edit_bank(bank_id):
    shop_identifier = current_shop_identifier()
    bank_oid = to_object_id(bank_id)
    if not bank_oid:
        abort(404)
    bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": shop_identifier})

    if not bank:
        abort(404)

    if request.method == "POST":
        verify_csrf()
        bank_name = (request.form.get("bank_name") or "").strip()
        opening_balance = parse_non_negative_float(request.form.get("opening_balance"))
        
        if not bank_name or len(bank_name) > 60:
            return render_template("edit_bank.html", bank=bank, error="Enter a valid bank name")
        
        # Check for duplicate bank name (case-insensitive) - excluding current bank
        existing_bank = banks_col.find_one({
            "shop_identifier": shop_identifier,
            "name": {"$regex": f"^{re.escape(bank_name)}$", "$options": "i"},
            "_id": {"$ne": bank_oid}
        })
        if existing_bank:
            return render_template("edit_bank.html", bank=bank, error="Bank name already exists")
        
        if opening_balance is None:
            return render_template("edit_bank.html", bank=bank, error="Opening balance must be a non-negative number")

        banks_col.update_one(
            {"_id": bank_oid},
            {"$set": {"name": bank_name, "opening_balance": opening_balance}}
        )
        recalculate_bank_balances(bank_id)
        flash('Bank updated successfully!', 'success')
        return redirect(url_for("add_bank"))

    return render_template("edit_bank.html", bank=bank)


@app.route("/delete-bank/<bank_id>", methods=["POST"])
def delete_bank(bank_id):
    verify_csrf()
    shop_identifier = current_shop_identifier()
    bank_oid = to_object_id(bank_id)
    if not bank_oid:
        return redirect(url_for("add_bank"))
    
    # Verify bank belongs to user
    bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": shop_identifier})
    if bank:
        # Delete entries associated with this bank first
        entries_col.delete_many({"bank_id": str(bank["_id"]), "shop_identifier": shop_identifier})
        # Delete the bank
        banks_col.delete_one({"_id": bank_oid})
    
    return redirect(url_for("add_bank"))


# -----------------------------
# ADD DAILY ENTRY
# -----------------------------
@app.route("/add-entry", methods=["GET", "POST"])
def add_entry():
    error = None
    today = date.today().isoformat()
    banks = get_shop_banks()
    selected_bank = request.args.get("selected_bank")

    if request.method == "POST":
        verify_csrf()
        bank_id = request.form.get("bank_id")
        entry_date = request.form.get("entry_date")

        if not bank_id:
            error = "Please select a bank"
        else:
            credited = parse_non_negative_float(request.form.get("credited") or 0)
            debited = parse_non_negative_float(request.form.get("debited") or 0)

            if credited is None or debited is None:
                error = "Amounts must be non-negative numbers"
            elif credited > 0 and debited > 0:
                error = "Enter either credited or debited amount, not both"
            elif credited == 0 and debited == 0:
                error = "Please enter credited or debited amount"
            else:
                bank_oid = to_object_id(bank_id)
                if not bank_oid:
                    error = "Invalid bank selection"
                    return render_daily_entry_page(banks=banks, today=today, error=error)

                if not entry_date:
                    error = "Please select a valid date"
                    return render_daily_entry_page(banks=banks, today=today, error=error)

                bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": current_shop_identifier()})
                if not bank:
                    error = "Invalid bank selection"
                    return render_daily_entry_page(banks=banks, today=today, error=error)

                # Create datetime using selected date + current time.
                try:
                    entry_datetime = datetime.strptime(
                        f"{entry_date} {datetime.now().strftime('%H:%M:%S')}",
                        "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError:
                    error = "Please select a valid date"
                    return render_daily_entry_page(banks=banks, today=today, error=error)

                last_entry = entries_col.find_one(
                    {
                        "bank_id": bank_id,
                        "date": {"$lte": entry_date},
                        "shop_identifier": current_shop_identifier()
                    },
                    sort=[("entry_datetime", -1)]
                )

                opening_balance = (
                    last_entry["remaining_balance"]
                    if last_entry else bank["opening_balance"]
                )

                remaining_balance = opening_balance + credited - debited

                entries_col.insert_one({
                    "date": entry_date,
                    "time": entry_datetime.strftime("%H:%M:%S"),
                    "entry_datetime": entry_datetime,
                    "bank_id": bank_id,
                    "bank_name": bank["name"],
                    "opening_balance": opening_balance,
                    "credited": credited,
                    "debited": debited,
                    "remaining_balance": remaining_balance,
                    "shop_identifier": current_shop_identifier()
                })

                # Recalculate balances to ensure consistency, especially for backdated entries
                recalculate_bank_balances(bank_id)
                
                if credited > 0:
                    flash(f"{credited} credited to {bank['name']}", "success")
                else:
                    flash(f"{debited} debited from {bank['name']}", "debit")
                    
                return redirect(url_for("add_entry", selected_bank=bank_id))

    from_date = (date.today() - timedelta(days=6)).isoformat()
    entries = list(
        entries_col.find({
            "shop_identifier": current_shop_identifier(),
            "date": {"$gte": from_date}
        })
        .sort([("date", -1), ("time", -1)])
    )

    return render_daily_entry_page(
        banks=banks,
        entries=entries,
        error=error,
        today=today,
        selected_bank=selected_bank
    )


# -----------------------------
# AVAILABLE BALANCE API (DATE-AWARE)
# -----------------------------
@app.route("/bank-balance/<bank_id>/<entry_date>")
def bank_balance(bank_id, entry_date):
    bank_oid = to_object_id(bank_id)
    if not bank_oid:
        return jsonify({"balance": 0})
    bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": current_shop_identifier()})
    if not bank:
        return jsonify({"balance": 0})

    last_entry = entries_col.find_one(
        {
            "bank_id": bank_id,
            "date": {"$lte": entry_date},
            "shop_identifier": current_shop_identifier()
        },
        sort=[("entry_datetime", -1)]
    )

    balance = (
        last_entry["remaining_balance"]
        if last_entry else bank["opening_balance"]
    )

    return jsonify({"balance": balance})


# -----------------------------
# EDIT ENTRY (SAME-DAY ONLY)
# -----------------------------
@app.route("/edit-entry/<entry_id>", methods=["GET", "POST"])
def edit_entry(entry_id):
    entry_oid = to_object_id(entry_id)
    if not entry_oid:
        return "Entry not found", 404
    entry = entries_col.find_one({"_id": entry_oid, "shop_identifier": current_shop_identifier()})
    if not entry:
        return "Entry not found", 404
    today = date.today().isoformat()

    if entry["date"] != today:
        return "Editing past entries is not allowed", 403

    if request.method == "POST":
        verify_csrf()
        credited = parse_non_negative_float(request.form.get("credited") or 0)
        debited = parse_non_negative_float(request.form.get("debited") or 0)

        if credited is None or debited is None:
            return "Amounts must be non-negative numbers", 400
        if credited > 0 and debited > 0:
            return "Enter either credited or debited amount, not both", 400

        entries_col.update_one(
            {"_id": entry_oid},
            {"$set": {"credited": credited, "debited": debited}}
        )

        recalculate_bank_balances(entry["bank_id"])
        
        if credited > 0:
            flash(f"Updated: {credited} credited to {entry['bank_name']}", "success")
        else:
            flash(f"Updated: {debited} debited from {entry['bank_name']}", "debit")
            
        return redirect(url_for("add_entry"))

    return render_template("edit_entry.html", entry=entry)


# -----------------------------
# DELETE ENTRY (SAME-DAY ONLY)
# -----------------------------
@app.route("/delete-entry/<entry_id>", methods=["POST"])
def delete_entry(entry_id):
    verify_csrf()
    entry_oid = to_object_id(entry_id)
    if not entry_oid:
        return "Entry not found", 404
    entry = entries_col.find_one({"_id": entry_oid, "shop_identifier": current_shop_identifier()})
    if not entry:
        return "Entry not found", 404
    today = date.today().isoformat()

    if entry["date"] != today:
        return "Deleting past entries is not allowed", 403

    entries_col.delete_one({"_id": entry_oid})
    recalculate_bank_balances(entry["bank_id"])
    flash('Entry deleted successfully!', 'success')
    return redirect(url_for("add_entry"))


# -----------------------------
# DAILY REPORT
# -----------------------------
@app.route("/daily-report")
def daily_report():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    entries = []

    if start_date and end_date:
        entries = get_entries_in_range(start_date, end_date)
        
        # Sort entries by date/time desc for the detailed table
        entries.sort(key=lambda x: (x['date'], x['time']), reverse=True)

    return render_template(
        "daily_report.html",
        entries=entries,
        start_date=start_date,
        end_date=end_date
    )


# -----------------------------
# MONTHLY REPORT
# -----------------------------
@app.route("/monthly-report")
def monthly_report():
    report_month = request.args.get("report_month")
    report = None
    bank_wise = []

    if report_month:
        entries = list(entries_col.find({
            "date": {"$regex": f"^{report_month}"},
            "shop_identifier": current_shop_identifier()
        }))
        report, bank_wise = build_report(entries)
        report["month_closing_balance"] = report.pop("closing_balance")

    return render_template(
        "monthly_report.html",
        report=report,
        bank_wise=bank_wise,
        selected_month=report_month
    )


# -----------------------------
# REPORTS HUB
# -----------------------------
@app.route("/reports")
def reports():
    return render_template("reports.html")


# -----------------------------
# WEEKLY REPORT
# -----------------------------
@app.route("/weekly-report")
def weekly_report():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    start_date = monday.isoformat()
    end_date = today.isoformat()

    entries = get_entries_in_range(start_date, end_date)
    report, bank_wise = build_report(entries)
    report["week_closing_balance"] = report.pop("closing_balance")

    return render_template(
        "weekly_report.html",
        report=report,
        bank_wise=bank_wise,
        start_date=start_date,
        end_date=end_date
    )


# -----------------------------
# CUSTOM REPORT (DATE RANGE)
# -----------------------------
@app.route("/custom-report")
def custom_report():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    report = None
    bank_wise = []

    if start_date and end_date:
        entries = get_entries_in_range(start_date, end_date)
        report, bank_wise = build_report(entries)
        report["range_closing_balance"] = report.pop("closing_balance")

    return render_template(
        "custom_report.html",
        report=report,
        bank_wise=bank_wise,
        start_date=start_date,
        end_date=end_date
    )


# -----------------------------
# RUN APP
# -----------------------------
if __name__ == "__main__":
    app.run(host="192.168.0.101", port=5000, debug=True)
