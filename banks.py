import re
from flask import current_app, flash, redirect, render_template, request, url_for
from pymongo import UpdateOne
from pymongo.errors import PyMongoError
from utils import parse_entry_datetime


def register_bank_routes(
    app,
    banks_col,
    entries_col,
    shops_col,
    parse_non_negative_float,
    check_password_hash_fn,
    to_object_id,
    verify_csrf,
    current_shop_identifier,
):
    def get_shop_banks():
        try:
            return list(banks_col.find({"shop_identifier": current_shop_identifier()}))
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading banks: {e}")
            return []

    def render_add_bank_page(error=None, edit_bank=None, form_values=None):
        if form_values is None:
            form_values = {}
        return render_template(
            "add_bank.html",
            banks=get_shop_banks(),
            error=error,
            edit_bank=edit_bank,
            form_values=form_values,
        )

    def get_current_shop():
        identifier = current_shop_identifier()
        if not identifier:
            return None
        try:
            return shops_col.find_one({
                "$or": [
                    {"identifier": identifier},
                    {"mobile": identifier},
                    {"email": identifier},
                ]
            })
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading current shop: {e}")
            return None

    def recalculate_bank_balances_from_date(bank_id, start_date):
        try:
            shop_identifier = current_shop_identifier()
            oid = to_object_id(bank_id)
            if not oid:
                return

            bank = banks_col.find_one({"_id": oid, "shop_identifier": shop_identifier})
            if not bank:
                return

            str_bank_id = str(bank_id)
            prev_entry = entries_col.find_one(
                {
                    "bank_id": str_bank_id,
                    "shop_identifier": shop_identifier,
                    "date": {"$lt": start_date},
                },
                sort=[("entry_datetime", -1)],
            )
            base_balance = (
                float(prev_entry.get("remaining_balance", bank["opening_balance"]))
                if prev_entry else float(bank["opening_balance"])
            )
            base_balance = max(0.0, base_balance)

            raw_entries = list(entries_col.find({
                "bank_id": str_bank_id,
                "shop_identifier": shop_identifier,
                "date": {"$gte": start_date},
            }))
            entries = sorted(raw_entries, key=parse_entry_datetime)

            balance = base_balance
            bulk_ops = []

            for e in entries:
                try:
                    credited = float(e.get("credited", 0))
                except Exception:
                    credited = 0.0
                credited = max(0.0, credited)

                try:
                    debited = float(e.get("debited", 0))
                except Exception:
                    debited = 0.0
                debited = max(0.0, debited)

                opening_balance = max(0.0, balance)
                balance = max(0.0, opening_balance + credited - debited)
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
            current_app.logger.error(f"Database error while recalculating balances from date: {e}")

    @app.route("/add-bank", methods=["GET", "POST"])
    def add_bank():
        shop_identifier = current_shop_identifier()

        if request.method == "POST":
            verify_csrf()
            edit_bank_id = (request.form.get("edit_bank_id") or "").strip()
            bank_name = (request.form.get("bank_name") or "").strip()
            opening_balance_raw = request.form.get("opening_balance")
            opening_balance = parse_non_negative_float(opening_balance_raw)
            form_values = {
                "bank_name": bank_name,
                "opening_balance": opening_balance_raw or "",
            }

            if edit_bank_id:
                bank_oid = to_object_id(edit_bank_id)
                if not bank_oid:
                    return render_add_bank_page(
                        error="Invalid bank selection",
                        form_values=form_values,
                    )

                try:
                    bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": shop_identifier})
                except PyMongoError as e:
                    current_app.logger.error(f"Database error while loading bank for inline edit: {e}")
                    flash("Database error occurred. Please try again.", "danger")
                    return render_add_bank_page(
                        error="Database error occurred. Please try again.",
                        form_values=form_values,
                    )

                if not bank:
                    return render_add_bank_page(
                        error="Bank not found",
                        form_values=form_values,
                    )

                if not bank_name or len(bank_name) > 60:
                    return render_add_bank_page(
                        error="Enter a valid bank name",
                        edit_bank=bank,
                        form_values=form_values,
                    )

                try:
                    existing_bank = banks_col.find_one({
                        "shop_identifier": shop_identifier,
                        "name": {"$regex": f"^{re.escape(bank_name)}$", "$options": "i"},
                        "_id": {"$ne": bank_oid},
                    })
                except PyMongoError as e:
                    current_app.logger.error(f"Database error while checking bank duplicate (inline edit): {e}")
                    flash("Database error occurred. Please try again.", "danger")
                    return render_add_bank_page(
                        error="Database error occurred. Please try again.",
                        edit_bank=bank,
                        form_values=form_values,
                    )
                if existing_bank:
                    return render_add_bank_page(
                        error="Bank name already exists",
                        edit_bank=bank,
                        form_values=form_values,
                    )

                if opening_balance is None:
                    return render_add_bank_page(
                        error="Opening balance must be a non-negative number",
                        edit_bank=bank,
                        form_values=form_values,
                    )

                try:
                    banks_col.update_one(
                        {"_id": bank_oid},
                        {"$set": {"name": bank_name, "opening_balance": opening_balance}},
                    )
                    earliest_entry = entries_col.find_one(
                        {"bank_id": str(bank["_id"]), "shop_identifier": shop_identifier},
                        sort=[("entry_datetime", 1)],
                    )
                    if earliest_entry:
                        recalculate_bank_balances_from_date(edit_bank_id, earliest_entry["date"])
                except PyMongoError as e:
                    current_app.logger.error(f"Database error while updating bank (inline edit): {e}")
                    flash("Database error occurred. Please try again.", "danger")
                    return render_add_bank_page(
                        error="Database error occurred. Please try again.",
                        edit_bank=bank,
                        form_values=form_values,
                    )

                flash("Bank updated successfully!", "success")
                return redirect(url_for("add_bank"))

            if not bank_name or len(bank_name) > 60:
                return render_add_bank_page(
                    error="Enter a valid bank name",
                    form_values=form_values,
                )

            try:
                existing_bank = banks_col.find_one({
                    "shop_identifier": shop_identifier,
                    "name": {"$regex": f"^{re.escape(bank_name)}$", "$options": "i"},
                })
            except PyMongoError as e:
                current_app.logger.error(f"Database error while checking bank duplicate: {e}")
                flash("Database error occurred. Please try again.", "danger")
                return render_add_bank_page(
                    error="Database error occurred. Please try again.",
                    form_values=form_values,
                )
            if existing_bank:
                return render_add_bank_page(
                    error="Bank name already exists",
                    form_values=form_values,
                )

            if opening_balance is None:
                return render_add_bank_page(
                    error="Opening balance must be a non-negative number",
                    form_values=form_values,
                )

            try:
                result = banks_col.insert_one({
                    "name": bank_name,
                    "opening_balance": opening_balance,
                    "shop_identifier": shop_identifier,
                })
                if not result.inserted_id:
                    flash("Failed to add bank.", "danger")
                    return render_add_bank_page(
                        error="Failed to add bank.",
                        form_values=form_values,
                    )
            except PyMongoError as e:
                current_app.logger.error(f"Database error while adding bank: {e}")
                flash("Database error occurred. Please try again.", "danger")
                return render_add_bank_page(
                    error="Database error occurred. Please try again.",
                    form_values=form_values,
                )
            flash(f"'{bank_name}' Bank added successfully!", "success")
            return redirect(url_for("add_bank"))

        edit_id = (request.args.get("edit_id") or "").strip()
        if not edit_id:
            return render_add_bank_page()

        bank_oid = to_object_id(edit_id)
        if not bank_oid:
            flash("Invalid bank selection", "danger")
            return redirect(url_for("add_bank"))

        try:
            edit_bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": shop_identifier})
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading bank for edit mode: {e}")
            flash("Database error occurred. Please try again.", "danger")
            return redirect(url_for("add_bank"))

        if not edit_bank:
            flash("Bank not found", "danger")
            return redirect(url_for("add_bank"))

        return render_add_bank_page(edit_bank=edit_bank)

    @app.route("/edit-bank/<bank_id>")
    def edit_bank(bank_id):
        return redirect(url_for("add_bank", edit_id=bank_id))

    @app.route("/delete-bank/<bank_id>", methods=["POST"])
    def delete_bank(bank_id):
        verify_csrf()
        password = request.form.get("password") or ""
        shop_identifier = current_shop_identifier()
        bank_oid = to_object_id(bank_id)
        if not bank_oid:
            return redirect(url_for("add_bank"))

        shop = get_current_shop()
        if not shop:
            flash("Unable to verify account. Please log in again.", "danger")
            return redirect(url_for("login"))

        if not password or not check_password_hash_fn(shop.get("password_hash", ""), password):
            flash("Incorrect password. Bank was not deleted.", "danger")
            return redirect(url_for("add_bank"))

        try:
            bank = banks_col.find_one({"_id": bank_oid, "shop_identifier": shop_identifier})
            if bank:
                entries_col.delete_many({"bank_id": str(bank["_id"]), "shop_identifier": shop_identifier})
                banks_col.delete_one({"_id": bank_oid})
                bank_name = bank.get("name", "Bank")
                flash(f"'{bank_name}' bank deleted successfully.", "success")
        except PyMongoError as e:
            current_app.logger.error(f"Database error while deleting bank: {e}")
            flash("Database error occurred. Please try again.", "danger")
            return redirect(url_for("add_bank"))

        return redirect(url_for("add_bank"))

    return {
        "get_shop_banks": get_shop_banks,
        "recalculate_bank_balances_from_date": recalculate_bank_balances_from_date,
    }
