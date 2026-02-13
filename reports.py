from datetime import date, datetime, timedelta
from flask import current_app, flash, render_template, request
from pymongo.errors import PyMongoError


def register_report_routes(app, entries_col, current_shop_identifier):
    def get_entries_in_range(start_date, end_date):
        try:
            return list(entries_col.find({
                "date": {"$gte": start_date, "$lte": end_date},
                "shop_identifier": current_shop_identifier(),
            }))
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading range entries: {e}")
            return []

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
            "closing_balance": sum(b["closing_balance"] for b in bank_wise),
        }
        return report, bank_wise

    @app.route("/daily-report")
    def daily_report():
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        entries = []

        if start_date and end_date:
            entries = get_entries_in_range(start_date, end_date)
            entries.sort(key=lambda x: (x["date"], x["time"]), reverse=True)

        return render_template(
            "daily_report.html",
            entries=entries,
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

    @app.route("/reports")
    def reports():
        return render_template("reports.html")

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
