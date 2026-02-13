import secrets
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort, flash
from werkzeug.security import generate_password_hash, check_password_hash
import re
from datetime import date, datetime, timedelta
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson.objectid import ObjectId


import os
from dotenv import load_dotenv
from banks import register_bank_routes
from reports import register_report_routes

# Load environment variables from .env file (for local development)
load_dotenv()

# -----------------------------
# FLASK INIT
# -----------------------------
app = Flask(__name__)
# Use SECRET_KEY from environment or fall back to a random one (not recommended for production persistence)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),
)


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
# SHARED APP HELPERS
# (used by auth + entries in this file)
# -----------------------------
def current_shop_identifier():
    return session.get("shop_identifier")


def find_shop_by_identifier(identifier):
    try:
        return shops_col.find_one({
            "$or": [
                {"identifier": identifier},
                {"mobile": identifier},
                {"email": identifier}
            ]
        })
    except PyMongoError as e:
        app.logger.error(f"Database error while finding shop: {e}")
        return None


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
    if not value or len(value) < 8:
        return False
    has_upper = re.search(r"[A-Z]", value)
    has_lower = re.search(r"[a-z]", value)
    has_digit = re.search(r"\d", value)
    return bool(has_upper and has_lower and has_digit)


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
            error = "Password must be at least 8 characters and include uppercase, lowercase, and a number"
        elif not is_valid_shop_name(shop_name):
            error = "Shop name must be 2-60 characters"
        else:
            existing = find_shop_by_identifier(identifier)
            if existing:
                error = "Email or mobile already registered. Please log in."
            else:
                try:
                    result = shops_col.insert_one({
                        "name": shop_name,
                        "identifier": identifier,
                        "password_hash": generate_password_hash(password)
                    })
                    if not result.inserted_id:
                        error = "Failed to create account. Please try again."
                    else:
                        return redirect(url_for("login"))
                except PyMongoError as e:
                    app.logger.error(f"Database error during signup: {e}")
                    error = "Database error occurred. Please try again."

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
        elif not password:
            error = "Password is required"
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
# BANK ROUTES MODULE (banks.py)
# -----------------------------
bank_module = register_bank_routes(
    app=app,
    banks_col=banks_col,
    entries_col=entries_col,
    parse_non_negative_float=parse_non_negative_float,
    to_object_id=to_object_id,
    verify_csrf=verify_csrf,
    current_shop_identifier=current_shop_identifier,
)
get_shop_banks = bank_module["get_shop_banks"]
recalculate_bank_balances = bank_module["recalculate_bank_balances"]


# -----------------------------
# ENTRY ROUTES
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

                try:
                    bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": current_shop_identifier()})
                except PyMongoError as e:
                    app.logger.error(f"Database error while loading bank: {e}")
                    flash("Database error occurred. Please try again.", "danger")
                    return render_daily_entry_page(banks=banks, today=today, error="Database error occurred. Please try again.")
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

                try:
                    last_entry = entries_col.find_one(
                        {
                            "bank_id": bank_id,
                            "date": {"$lte": entry_date},
                            "shop_identifier": current_shop_identifier()
                        },
                        sort=[("entry_datetime", -1)]
                    )
                except PyMongoError as e:
                    app.logger.error(f"Database error while reading last entry: {e}")
                    flash("Database error occurred. Please try again.", "danger")
                    return render_daily_entry_page(banks=banks, today=today, error="Database error occurred. Please try again.")

                opening_balance = (
                    last_entry["remaining_balance"]
                    if last_entry else bank["opening_balance"]
                )

                remaining_balance = opening_balance + credited - debited

                try:
                    result = entries_col.insert_one({
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
                    if not result.inserted_id:
                        flash("Failed to save entry.", "danger")
                        return redirect(url_for("add_entry"))
                    recalculate_bank_balances(bank_id)
                except PyMongoError as e:
                    app.logger.error(f"Database error while creating entry: {e}")
                    flash("Database error occurred. Please try again.", "danger")
                    return redirect(url_for("add_entry"))
                
                if credited > 0:
                    flash(f"{credited} credited to {bank['name']}", "success")
                else:
                    flash(f"{debited} debited from {bank['name']}", "debit")
                    
                return redirect(url_for("add_entry", selected_bank=bank_id))

    from_date = (date.today() - timedelta(days=6)).isoformat()
    try:
        entries = list(
            entries_col.find({
                "shop_identifier": current_shop_identifier(),
                "date": {"$gte": from_date}
            })
            .sort([("date", -1), ("time", -1)])
        )
    except PyMongoError as e:
        app.logger.error(f"Database error while loading entries: {e}")
        entries = []
        flash("Database error occurred. Please try again.", "danger")

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
    try:
        bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": current_shop_identifier()})
    except PyMongoError as e:
        app.logger.error(f"Database error while reading bank balance bank: {e}")
        return jsonify({"balance": 0})
    if not bank:
        return jsonify({"balance": 0})

    try:
        last_entry = entries_col.find_one(
            {
                "bank_id": bank_id,
                "date": {"$lte": entry_date},
                "shop_identifier": current_shop_identifier()
            },
            sort=[("entry_datetime", -1)]
        )
    except PyMongoError as e:
        app.logger.error(f"Database error while reading bank balance entry: {e}")
        return jsonify({"balance": 0})

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
    try:
        entry = entries_col.find_one({"_id": entry_oid, "shop_identifier": current_shop_identifier()})
    except PyMongoError as e:
        app.logger.error(f"Database error while loading entry for edit: {e}")
        return "Database error occurred. Please try again.", 500
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

        try:
            entries_col.update_one(
                {"_id": entry_oid},
                {"$set": {"credited": credited, "debited": debited}}
            )
            recalculate_bank_balances(entry["bank_id"])
        except PyMongoError as e:
            app.logger.error(f"Database error while updating entry: {e}")
            flash("Database error occurred. Please try again.", "danger")
            return redirect(url_for("add_entry"))
        
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
    try:
        entry = entries_col.find_one({"_id": entry_oid, "shop_identifier": current_shop_identifier()})
    except PyMongoError as e:
        app.logger.error(f"Database error while loading entry for delete: {e}")
        flash("Database error occurred. Please try again.", "danger")
        return redirect(url_for("add_entry"))
    if not entry:
        return "Entry not found", 404
    today = date.today().isoformat()

    if entry["date"] != today:
        return "Deleting past entries is not allowed", 403

    try:
        entries_col.delete_one({"_id": entry_oid})
        recalculate_bank_balances(entry["bank_id"])
    except PyMongoError as e:
        app.logger.error(f"Database error while deleting entry: {e}")
        flash("Database error occurred. Please try again.", "danger")
        return redirect(url_for("add_entry"))
    flash('Entry deleted successfully!', 'success')
    return redirect(url_for("add_entry"))


# -----------------------------
# REPORT ROUTES MODULE (reports.py)
# -----------------------------
register_report_routes(
    app=app,
    entries_col=entries_col,
    current_shop_identifier=current_shop_identifier,
)


# -----------------------------
# RUN APP
# -----------------------------
if __name__ == "__main__":
    app.run(host="192.168.0.101", port=5000, debug=True)
