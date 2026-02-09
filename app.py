import secrets
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime
from pymongo import MongoClient
from bson.objectid import ObjectId


# -----------------------------
# FLASK INIT
# -----------------------------
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)


# -----------------------------
# MONGODB CONNECTION (LOCAL)
# -----------------------------
client = MongoClient("mongodb://localhost:27017/")
db = client["daily_ledger_db"]

banks_col = db["banks"]
entries_col = db["daily_entries"]
shops_col = db["shops"]


# -----------------------------
# HELPER: RECALCULATE BALANCES
# -----------------------------
def recalculate_bank_balances(bank_id):
    """
    Recalculate opening & remaining balances
    using entry_datetime (correct order)
    """
    shop_identifier = session.get("shop_identifier")
    bank = banks_col.find_one({"_id": ObjectId(bank_id), "shop_identifier": shop_identifier})
    if not bank:
        return

    entries = list(
        entries_col.find({"bank_id": bank_id, "shop_identifier": shop_identifier})
        .sort([("entry_datetime", 1)])
    )

    balance = bank["opening_balance"]

    for e in entries:
        opening_balance = balance
        balance = balance + e["credited"] - e["debited"]

        entries_col.update_one(
            {"_id": e["_id"]},
            {"$set": {
                "opening_balance": opening_balance,
                "remaining_balance": balance
            }}
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

        if not identifier:
            error = "Email or mobile number is required"
        elif not password:
            error = "Password is required"
        elif not shop_name:
            error = "Shop name is required"
        else:
            existing = shops_col.find_one({
                "$or": [
                    {"identifier": identifier},
                    {"mobile": identifier},
                    {"email": identifier}
                ]
            })
            if existing:
                error = "Email or mobile already registered. Please log in."
            else:
                shops_col.insert_one({
                    "name": shop_name,
                    "identifier": identifier,
                    "password_hash": generate_password_hash(password)
                })
                return redirect(url_for("login"))

    return render_template("signup.html", error=error, csrf_token=get_csrf_token())


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        verify_csrf()
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""
        if not identifier:
            error = "Email or mobile number is required"
        elif not password:
            error = "Password is required"
        else:
            existing = shops_col.find_one({
                "$or": [
                    {"identifier": identifier},
                    {"mobile": identifier},
                    {"email": identifier}
                ]
            })
            if not existing or not check_password_hash(existing.get("password_hash", ""), password):
                error = "Invalid email/mobile or password"
            else:
                session["shop_name"] = existing.get("name")
                session["shop_identifier"] = identifier
                return redirect(url_for("home"))

    return render_template("login.html", error=error, csrf_token=get_csrf_token())


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
        banks_col.insert_one({
            "name": request.form.get("bank_name"),
            "opening_balance": float(request.form.get("opening_balance")),
            "shop_identifier": session.get("shop_identifier")
        })
        return redirect(url_for("add_bank"))

    banks = list(banks_col.find({"shop_identifier": session.get("shop_identifier")}))
    return render_template("add_bank.html", banks=banks, csrf_token=get_csrf_token())


# -----------------------------
# ADD DAILY ENTRY
# -----------------------------
@app.route("/add-entry", methods=["GET", "POST"])
def add_entry():
    error = None
    today = date.today().isoformat()
    banks = list(banks_col.find({"shop_identifier": session.get("shop_identifier")}))

    if request.method == "POST":
        verify_csrf()
        bank_id = request.form.get("bank_id")
        entry_date = request.form.get("entry_date")

        if not bank_id:
            error = "Please select a bank"
        else:
            credited = float(request.form.get("credited") or 0)
            debited = float(request.form.get("debited") or 0)

            if credited < 0 or debited < 0:
                error = "Amounts cannot be negative"
            elif credited > 0 and debited > 0:
                error = "Enter either credited or debited amount, not both"
            elif credited == 0 and debited == 0:
                error = "Please enter credited or debited amount"
            else:
                bank = banks_col.find_one({"_id": ObjectId(bank_id), "shop_identifier": session.get("shop_identifier")})
                if not bank:
                    error = "Invalid bank selection"
                    return render_template(
                        "daily_entry.html",
                        banks=banks,
                        entries=[],
                        error=error,
                        today=today,
                        csrf_token=get_csrf_token()
                    )

                # 🔑 FIX: create datetime using selected date + current time
                entry_datetime = datetime.strptime(
                    f"{entry_date} {datetime.now().strftime('%H:%M:%S')}",
                    "%Y-%m-%d %H:%M:%S"
                )

                last_entry = entries_col.find_one(
                    {
                        "bank_id": bank_id,
                        "date": {"$lte": entry_date},
                        "shop_identifier": session.get("shop_identifier")
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
                    "entry_datetime": entry_datetime,   # ✅ critical fix
                    "bank_id": bank_id,
                    "bank_name": bank["name"],
                    "opening_balance": opening_balance,
                    "credited": credited,
                    "debited": debited,
                    "remaining_balance": remaining_balance,
                    "shop_identifier": session.get("shop_identifier")
                })

                return redirect(url_for("add_entry"))

    entries = list(
        entries_col.find({"shop_identifier": session.get("shop_identifier")})
        .sort([("entry_datetime", -1)])
    )

    return render_template(
        "daily_entry.html",
        banks=banks,
        entries=entries,
        error=error,
        today=today,
        csrf_token=get_csrf_token()
    )


# -----------------------------
# AVAILABLE BALANCE API (DATE-AWARE)
# -----------------------------
@app.route("/bank-balance/<bank_id>/<entry_date>")
def bank_balance(bank_id, entry_date):
    bank = banks_col.find_one({"_id": ObjectId(bank_id), "shop_identifier": session.get("shop_identifier")})
    if not bank:
        return jsonify({"balance": 0})

    last_entry = entries_col.find_one(
        {
            "bank_id": bank_id,
            "date": {"$lte": entry_date},
            "shop_identifier": session.get("shop_identifier")
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
    entry = entries_col.find_one({"_id": ObjectId(entry_id), "shop_identifier": session.get("shop_identifier")})
    if not entry:
        return "Entry not found", 404
    today = date.today().isoformat()

    if entry["date"] != today:
        return "Editing past entries is not allowed", 403

    if request.method == "POST":
        verify_csrf()
        credited = float(request.form.get("credited") or 0)
        debited = float(request.form.get("debited") or 0)

        if credited < 0 or debited < 0:
            return "Amounts cannot be negative", 400
        if credited > 0 and debited > 0:
            return "Enter either credited or debited amount, not both", 400

        entries_col.update_one(
            {"_id": ObjectId(entry_id)},
            {"$set": {"credited": credited, "debited": debited}}
        )

        recalculate_bank_balances(entry["bank_id"])
        return redirect(url_for("add_entry"))

    return render_template("edit_entry.html", entry=entry, csrf_token=get_csrf_token())


# -----------------------------
# DELETE ENTRY (SAME-DAY ONLY)
# -----------------------------
@app.route("/delete-entry/<entry_id>", methods=["POST"])
def delete_entry(entry_id):
    verify_csrf()
    entry = entries_col.find_one({"_id": ObjectId(entry_id), "shop_identifier": session.get("shop_identifier")})
    if not entry:
        return "Entry not found", 404
    today = date.today().isoformat()

    if entry["date"] != today:
        return "Deleting past entries is not allowed", 403

    entries_col.delete_one({"_id": ObjectId(entry_id)})
    recalculate_bank_balances(entry["bank_id"])

    return redirect(url_for("add_entry"))


# -----------------------------
# DAILY REPORT
# -----------------------------
@app.route("/daily-report")
def daily_report():
    report_date = request.args.get("report_date")
    report = None
    bank_wise = []

    if report_date:
        entries = list(entries_col.find({
            "date": report_date,
            "shop_identifier": session.get("shop_identifier")
        }))

        def entry_dt(e):
            dt = e.get("entry_datetime")
            if dt:
                return dt
            d = e.get("date")
            t = e.get("time") or "00:00:00"
            try:
                return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
            except Exception:
                return datetime.min

        total_credit = sum(e["credited"] for e in entries)
        total_debit = sum(e["debited"] for e in entries)

        summary = {}
        for e in entries:
            b = e["bank_name"]
            e_dt = entry_dt(e)
            summary.setdefault(b, {"credit": 0, "debit": 0, "close": e["remaining_balance"], "dt": e_dt})
            summary[b]["credit"] += e["credited"]
            summary[b]["debit"] += e["debited"]
            if e_dt >= summary[b]["dt"]:
                summary[b]["close"] = e["remaining_balance"]
                summary[b]["dt"] = e_dt

        for b, d in summary.items():
            bank_wise.append({
                "bank": b,
                "total_credit": d["credit"],
                "total_debit": d["debit"],
                "closing_balance": d["close"]
            })

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
            "day_closing_balance": sum(b["closing_balance"] for b in bank_wise)
        }

    return render_template(
        "daily_report.html",
        report=report,
        bank_wise=bank_wise,
        selected_date=report_date
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
            "shop_identifier": session.get("shop_identifier")
        }))

        def entry_dt(e):
            dt = e.get("entry_datetime")
            if dt:
                return dt
            d = e.get("date")
            t = e.get("time") or "00:00:00"
            try:
                return datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
            except Exception:
                return datetime.min

        total_credit = sum(e["credited"] for e in entries)
        total_debit = sum(e["debited"] for e in entries)

        summary = {}
        for e in entries:
            b = e["bank_name"]
            e_dt = entry_dt(e)
            summary.setdefault(b, {"credit": 0, "debit": 0, "close": e["remaining_balance"], "dt": e_dt})
            summary[b]["credit"] += e["credited"]
            summary[b]["debit"] += e["debited"]
            if e_dt >= summary[b]["dt"]:
                summary[b]["close"] = e["remaining_balance"]
                summary[b]["dt"] = e_dt

        for b, d in summary.items():
            bank_wise.append({
                "bank": b,
                "total_credit": d["credit"],
                "total_debit": d["debit"],
                "closing_balance": d["close"]
            })

        top_banks = sorted(bank_wise, key=lambda x: x["total_credit"] + x["total_debit"], reverse=True)
        most_used = top_banks[0]["bank"] if top_banks else "N/A"

        report = {
            "total_credit": total_credit,
            "total_debit": total_debit,
            "most_used_bank": most_used,
            "highest_amount": max(
                [max(e["credited"], e["debited"]) for e in entries],
                default=0
            ),
            "month_closing_balance": sum(b["closing_balance"] for b in bank_wise)
        }

    return render_template(
        "monthly_report.html",
        report=report,
        bank_wise=bank_wise,
        selected_month=report_month
    )


# -----------------------------
# RUN APP
# -----------------------------
if __name__ == "__main__":
    app.run(host= '192.168.0.101', debug=True)
