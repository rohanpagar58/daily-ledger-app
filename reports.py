import re
from datetime import date, timedelta
from flask import current_app, flash, redirect, render_template, request, url_for
from pymongo.errors import PyMongoError
from utils import group_entries_by_date, parse_entry_datetime


def register_report_routes(
    app,
    entries_col,
    current_shop_identifier,
    shops_col,
    verify_csrf,
    check_password_hash_fn,
    recalculate_bank_balances_from_date,
):
    def get_entries_in_range(start_date, end_date):
        try:
            return list(entries_col.find({
                "date": {"$gte": start_date, "$lte": end_date},
                "shop_identifier": current_shop_identifier(),
            }))
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading range entries: {e}")
            return []

    def build_report(entries):
        total_credit = sum(e["credited"] for e in entries)
        total_debit = sum(e["debited"] for e in entries)

        summary = {}
        for e in entries:
            bank_name = e["bank_name"]
            e_dt = e.get("entry_datetime") or parse_entry_datetime(e)
            summary.setdefault(bank_name, {"credit": 0, "debit": 0, "close": e["remaining_balance"], "dt": e_dt})
            summary[bank_name]["credit"] += e["credited"]
            summary[bank_name]["debit"] += e["debited"]
            if e_dt >= summary[bank_name]["dt"]:
                summary[bank_name]["close"] = e["remaining_balance"]
                summary[bank_name]["dt"] = e_dt

        bank_wise = []
        for bank_name, values in summary.items():
            bank_wise.append({
                "bank": bank_name,
                "total_credit": values["credit"],
                "total_debit": values["debit"],
                "closing_balance": values["close"],
            })
        bank_wise.sort(key=lambda x: x["bank"].lower() if isinstance(x["bank"], str) else "")

        most_used = (
            max(bank_wise, key=lambda x: x["total_credit"] + x["total_debit"])["bank"]
            if bank_wise else "N/A"
        )

        report = {
            "total_credit": total_credit,
            "total_debit": total_debit,
            "most_used_bank": most_used,
            "closing_balance": sum(b["closing_balance"] for b in bank_wise),
        }
        return report, bank_wise

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

    @app.route("/daily-report")
    def daily_report():
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        grouped_entries = []

        if start_date and end_date:
            entries = get_entries_in_range(start_date, end_date)
            entries.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
            grouped_entries = group_entries_by_date(entries)

        return render_template(
            "daily_report.html",
            grouped_entries=grouped_entries,
            start_date=start_date,
            end_date=end_date,
        )

    @app.route("/monthly-report")
    def monthly_report():
        report_month = request.args.get("report_month")
        report = None
        bank_wise = []

        if report_month:
            try:
                entries = list(entries_col.find({
                    "date": {"$regex": f"^{report_month}"},
                    "shop_identifier": current_shop_identifier(),
                }))
                report, bank_wise = build_report(entries)
                report["month_closing_balance"] = report.pop("closing_balance")
            except PyMongoError as e:
                current_app.logger.error(f"Database error while loading monthly report: {e}")
                flash("Database error occurred. Please try again.", "danger")
                report = None
                bank_wise = []

        return render_template(
            "monthly_report.html",
            report=report,
            bank_wise=bank_wise,
            selected_month=report_month,
        )

    @app.route("/yearly-report")
    def yearly_report():
        report_year = request.args.get("report_year")
        selected_year = report_year or str(date.today().year)
        report = None
        bank_wise = []

        if report_year:
            try:
                entries = list(entries_col.find({
                    "date": {"$regex": f"^{report_year}-"},
                    "shop_identifier": current_shop_identifier(),
                }))
                report, bank_wise = build_report(entries)
                report["year_closing_balance"] = report.pop("closing_balance")
            except PyMongoError as e:
                current_app.logger.error(f"Database error while loading yearly report: {e}")
                flash("Database error occurred. Please try again.", "danger")
                report = None
                bank_wise = []

        return render_template(
            "yearly_report.html",
            report=report,
            bank_wise=bank_wise,
            selected_year=selected_year,
        )

    @app.route("/reports")
    def reports():
        return render_template("reports.html")

    @app.route("/reports/delete-data", methods=["POST"])
    def delete_report_data():
        verify_csrf()

        delete_type = (request.form.get("delete_type") or "").strip().lower()
        period_value = (request.form.get("period_value") or "").strip()
        password = request.form.get("password") or ""

        if delete_type not in {"month", "year"}:
            flash("Please select month or year.", "danger")
            return redirect(url_for("reports"))

        if delete_type == "month" and not re.fullmatch(r"\d{4}-\d{2}", period_value):
            flash("Please select a valid month.", "danger")
            return redirect(url_for("reports"))

        if delete_type == "year" and not re.fullmatch(r"\d{4}", period_value):
            flash("Please select a valid year.", "danger")
            return redirect(url_for("reports"))

        shop = get_current_shop()
        if not shop:
            flash("Unable to verify account. Please log in again.", "danger")
            return redirect(url_for("login"))

        if not password or not check_password_hash_fn(shop.get("password_hash", ""), password):
            flash("Incorrect password. Data was not deleted.", "danger")
            return redirect(url_for("reports"))

        if delete_type == "month":
            date_regex = f"^{period_value}"
            period_label = f"Month {period_value}"
            recalc_start_date = f"{period_value}-01"
        else:
            date_regex = f"^{period_value}-"
            period_label = f"Year {period_value}"
            recalc_start_date = f"{period_value}-01-01"

        query = {
            "date": {"$regex": date_regex},
            "shop_identifier": current_shop_identifier(),
        }

        try:
            affected_bank_ids = entries_col.distinct("bank_id", query)
            delete_result = entries_col.delete_many(query)

            for bank_id in affected_bank_ids:
                try:
                    recalculate_bank_balances_from_date(bank_id, recalc_start_date)
                except Exception as recalc_error:
                    current_app.logger.error(
                        f"Balance recalc failed for bank_id={bank_id}: {recalc_error}"
                    )

            flash(
                f"{period_label} data deleted successfully. {delete_result.deleted_count} entries removed.",
                "success",
            )
        except PyMongoError as e:
            current_app.logger.error(f"Database error while deleting report data: {e}")
            flash("Database error occurred. Please try again.", "danger")

        return redirect(url_for("reports"))

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
            end_date=end_date,
        )

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
            end_date=end_date,
        )
