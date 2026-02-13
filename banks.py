import re
from datetime import datetime
from flask import abort, current_app, flash, redirect, render_template, request, url_for
from pymongo import UpdateOne
from pymongo.errors import PyMongoError


def register_bank_routes(
    app,
    banks_col,
    entries_col,
    parse_non_negative_float,
    to_object_id,
    verify_csrf,
    current_shop_identifier,
):
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

    def get_shop_banks():
        try:
            return list(banks_col.find({"shop_identifier": current_shop_identifier()}))
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading banks: {e}")
            return []

    def render_add_bank_page(error=None):
        return render_template("add_bank.html", banks=get_shop_banks(), error=error)

    def recalculate_bank_balances(bank_id):
        try:
            shop_identifier = current_shop_identifier()
            oid = to_object_id(bank_id)
            if not oid:
                return

            bank = banks_col.find_one({"_id": oid, "shop_identifier": shop_identifier})
            if not bank:
                return

            str_bank_id = str(bank_id)
            raw_entries = list(entries_col.find({"bank_id": str_bank_id, "shop_identifier": shop_identifier}))
            entries = sorted(raw_entries, key=parse_entry_datetime)

            balance = bank["opening_balance"]
            bulk_ops = []

            for e in entries:
                try:
                    credited = float(e.get("credited", 0))
                except Exception:
                    credited = 0.0

                try:
                    debited = float(e.get("debited", 0))
                except Exception:
                    debited = 0.0

                opening_balance = balance
                balance = balance + credited - debited
                correct_dt = parse_entry_datetime(e)

                updates = {
                    "opening_balance": opening_balance,
                    "remaining_balance": balance,
                    "credited": credited,
                    "debited": debited,
                    "entry_datetime": correct_dt,
                    "time": correct_dt.strftime("%H:%M:%S"),
                }

                bulk_ops.append(UpdateOne({"_id": e["_id"]}, {"$set": updates}))

            if bulk_ops:
                entries_col.bulk_write(bulk_ops)
        except PyMongoError as e:
            current_app.logger.error(f"Database error while recalculating balances: {e}")

    @app.route("/add-bank", methods=["GET", "POST"])
    def add_bank():
        if request.method == "POST":
            verify_csrf()
            bank_name = (request.form.get("bank_name") or "").strip()
            opening_balance = parse_non_negative_float(request.form.get("opening_balance"))
            if not bank_name or len(bank_name) > 60:
                return render_add_bank_page(error="Enter a valid bank name")

            try:
                existing_bank = banks_col.find_one({
                    "shop_identifier": current_shop_identifier(),
                    "name": {"$regex": f"^{re.escape(bank_name)}$", "$options": "i"},
                })
            except PyMongoError as e:
                current_app.logger.error(f"Database error while checking bank duplicate: {e}")
                flash("Database error occurred. Please try again.", "danger")
                return render_add_bank_page(error="Database error occurred. Please try again.")
            if existing_bank:
                return render_add_bank_page(error="Bank name already exists")

            if opening_balance is None:
                return render_add_bank_page(error="Opening balance must be a non-negative number")

            try:
                result = banks_col.insert_one({
                    "name": bank_name,
                    "opening_balance": opening_balance,
                    "shop_identifier": current_shop_identifier(),
                })
                if not result.inserted_id:
                    flash("Failed to add bank.", "danger")
                    return render_add_bank_page(error="Failed to add bank.")
            except PyMongoError as e:
                current_app.logger.error(f"Database error while adding bank: {e}")
                flash("Database error occurred. Please try again.", "danger")
                return render_add_bank_page(error="Database error occurred. Please try again.")
            flash(f"'{bank_name}' Bank added successfully!", "success")
            return redirect(url_for("add_bank"))

        return render_add_bank_page()

    @app.route("/edit-bank/<bank_id>", methods=["GET", "POST"])
    def edit_bank(bank_id):
        shop_identifier = current_shop_identifier()
        bank_oid = to_object_id(bank_id)
        if not bank_oid:
            abort(404)

        try:
            bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": shop_identifier})
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading bank for edit: {e}")
            flash("Database error occurred. Please try again.", "danger")
            return redirect(url_for("add_bank"))
        if not bank:
            abort(404)

        if request.method == "POST":
            verify_csrf()
            bank_name = (request.form.get("bank_name") or "").strip()
            opening_balance = parse_non_negative_float(request.form.get("opening_balance"))

            if not bank_name or len(bank_name) > 60:
                return render_template("edit_bank.html", bank=bank, error="Enter a valid bank name")

            try:
                existing_bank = banks_col.find_one({
                    "shop_identifier": shop_identifier,
                    "name": {"$regex": f"^{re.escape(bank_name)}$", "$options": "i"},
                    "_id": {"$ne": bank_oid},
                })
            except PyMongoError as e:
                current_app.logger.error(f"Database error while checking bank duplicate (edit): {e}")
                flash("Database error occurred. Please try again.", "danger")
                return render_template("edit_bank.html", bank=bank, error="Database error occurred. Please try again.")
            if existing_bank:
                return render_template("edit_bank.html", bank=bank, error="Bank name already exists")

            if opening_balance is None:
                return render_template("edit_bank.html", bank=bank, error="Opening balance must be a non-negative number")

            try:
                banks_col.update_one({"_id": bank_oid}, {"$set": {"name": bank_name, "opening_balance": opening_balance}})
                recalculate_bank_balances(bank_id)
            except PyMongoError as e:
                current_app.logger.error(f"Database error while updating bank: {e}")
                flash("Database error occurred. Please try again.", "danger")
                return render_template("edit_bank.html", bank=bank, error="Database error occurred. Please try again.")
            flash("Bank updated successfully!", "success")
            return redirect(url_for("add_bank"))

        return render_template("edit_bank.html", bank=bank)

    @app.route("/delete-bank/<bank_id>", methods=["POST"])
    def delete_bank(bank_id):
        verify_csrf()
        shop_identifier = current_shop_identifier()
        bank_oid = to_object_id(bank_id)
        if not bank_oid:
            return redirect(url_for("add_bank"))

        try:
            bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": shop_identifier})
            if bank:
                entries_col.delete_many({"bank_id": str(bank["_id"]), "shop_identifier": shop_identifier})
                banks_col.delete_one({"_id": bank_oid})
        except PyMongoError as e:
            current_app.logger.error(f"Database error while deleting bank: {e}")
            flash("Database error occurred. Please try again.", "danger")
            return redirect(url_for("add_bank"))

        return redirect(url_for("add_bank"))

    return {
        "get_shop_banks": get_shop_banks,
        "recalculate_bank_balances": recalculate_bank_balances,
    }
