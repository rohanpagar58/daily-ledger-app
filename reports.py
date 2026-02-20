import re
import os
from io import BytesIO
from datetime import date, datetime, timedelta
from flask import current_app, flash, redirect, render_template, request, send_file, url_for
from pymongo.errors import PyMongoError
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle
from utils import group_entries_by_date, parse_entry_datetime

try:
    from svglib.svglib import svg2rlg
except Exception:
    svg2rlg = None


def register_report_routes(
    app,
    entries_col,
    current_shop_identifier,
    shops_col,
    verify_csrf,
    check_password_hash_fn,
    recalculate_bank_balances_from_date,
):
    daily_projection = {
        "_id": 0,
        "date": 1,
        "time": 1,
        "bank_name": 1,
        "opening_balance": 1,
        "credited": 1,
        "debited": 1,
        "remaining_balance": 1,
    }
    summary_projection = {
        "_id": 0,
        "date": 1,
        "time": 1,
        "entry_datetime": 1,
        "bank_name": 1,
        "credited": 1,
        "debited": 1,
        "remaining_balance": 1,
    }

    def to_number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def normalize_date_range(start_date, end_date):
        try:
            start_obj = date.fromisoformat(start_date)
            end_obj = date.fromisoformat(end_date)
        except (TypeError, ValueError):
            return None, None
        if start_obj > end_obj:
            return None, None
        return start_obj.isoformat(), end_obj.isoformat()

    def get_month_date_range(report_month):
        if not re.fullmatch(r"\d{4}-\d{2}", report_month or ""):
            return None, None
        try:
            start_obj = date.fromisoformat(f"{report_month}-01")
        except ValueError:
            return None, None
        next_month = (start_obj.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_obj = next_month - timedelta(days=1)
        return start_obj.isoformat(), end_obj.isoformat()

    def get_year_date_range(report_year):
        if not re.fullmatch(r"\d{4}", report_year or ""):
            return None, None
        try:
            year_int = int(report_year)
            start_obj = date(year_int, 1, 1)
            end_obj = date(year_int, 12, 31)
        except ValueError:
            return None, None
        return start_obj.isoformat(), end_obj.isoformat()

    def get_daily_entries_in_range(start_date, end_date):
        try:
            return list(
                entries_col.find(
                    {
                        "date": {"$gte": start_date, "$lte": end_date},
                        "shop_identifier": current_shop_identifier(),
                    },
                    daily_projection,
                ).sort([("date", -1), ("time", -1), ("entry_datetime", -1)])
            )
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading day-wise entries: {e}")
            return []

    def get_day_wise_dates_in_range(start_date, end_date):
        try:
            dates = entries_col.distinct(
                "date",
                {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "shop_identifier": current_shop_identifier(),
                },
            )
            valid_dates = [d for d in dates if isinstance(d, str)]
            valid_dates.sort(reverse=True)
            return valid_dates
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading day-wise dates: {e}")
            return []

    def get_daily_entries_for_dates(selected_dates):
        if not selected_dates:
            return []
        try:
            return list(
                entries_col.find(
                    {
                        "date": {"$in": selected_dates},
                        "shop_identifier": current_shop_identifier(),
                    },
                    daily_projection,
                ).sort([("date", -1), ("time", -1), ("entry_datetime", -1)])
            )
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading paged day-wise entries: {e}")
            return []

    def get_summary_entries_in_range(start_date, end_date):
        try:
            return list(
                entries_col.find(
                    {
                        "date": {"$gte": start_date, "$lte": end_date},
                        "shop_identifier": current_shop_identifier(),
                    },
                    summary_projection,
                )
            )
        except PyMongoError as e:
            current_app.logger.error(f"Database error while loading report summary entries: {e}")
            return []

    def build_report(entries):
        total_credit = sum(to_number(e.get("credited", 0)) for e in entries)
        total_debit = sum(to_number(e.get("debited", 0)) for e in entries)

        summary = {}
        for e in entries:
            bank_name = e.get("bank_name") or "Unknown"
            credited = to_number(e.get("credited", 0))
            debited = to_number(e.get("debited", 0))
            remaining_balance = to_number(e.get("remaining_balance", 0))
            e_dt = e.get("entry_datetime") or parse_entry_datetime(e)
            summary.setdefault(bank_name, {"credit": 0.0, "debit": 0.0, "close": remaining_balance, "dt": e_dt})
            summary[bank_name]["credit"] += credited
            summary[bank_name]["debit"] += debited
            if e_dt >= summary[bank_name]["dt"]:
                summary[bank_name]["close"] = remaining_balance
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

    def build_report_aggregate(start_date, end_date):
        query = {
            "date": {"$gte": start_date, "$lte": end_date},
            "shop_identifier": current_shop_identifier(),
        }
        pipeline = [
            {"$match": query},
            {"$sort": {"bank_name": 1, "date": 1, "time": 1, "entry_datetime": 1}},
            {
                "$group": {
                    "_id": {"$ifNull": ["$bank_name", "Unknown"]},
                    "total_credit": {"$sum": {"$ifNull": ["$credited", 0]}},
                    "total_debit": {"$sum": {"$ifNull": ["$debited", 0]}},
                    "closing_balance": {"$last": {"$ifNull": ["$remaining_balance", 0]}},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "bank": "$_id",
                    "total_credit": 1,
                    "total_debit": 1,
                    "closing_balance": 1,
                }
            },
        ]

        bank_wise = list(entries_col.aggregate(pipeline, allowDiskUse=True))
        for row in bank_wise:
            row["bank"] = row.get("bank") or "Unknown"
            row["total_credit"] = to_number(row.get("total_credit"))
            row["total_debit"] = to_number(row.get("total_debit"))
            row["closing_balance"] = to_number(row.get("closing_balance"))

        bank_wise.sort(key=lambda x: x["bank"].lower() if isinstance(x["bank"], str) else "")
        if not bank_wise:
            return None, []

        total_credit = sum(b["total_credit"] for b in bank_wise)
        total_debit = sum(b["total_debit"] for b in bank_wise)
        most_used = max(bank_wise, key=lambda x: x["total_credit"] + x["total_debit"])["bank"]
        report = {
            "total_credit": total_credit,
            "total_debit": total_debit,
            "most_used_bank": most_used,
            "closing_balance": sum(b["closing_balance"] for b in bank_wise),
        }
        return report, bank_wise

    def build_report_for_range(start_date, end_date):
        try:
            return build_report_aggregate(start_date, end_date)
        except PyMongoError as e:
            current_app.logger.error(f"Database error while aggregating report range: {e}")
            entries = get_summary_entries_in_range(start_date, end_date)
            if not entries:
                return None, []
            return build_report(entries)

    def build_monthly_pdf(report_month, report, bank_wise):
        def format_amount(value):
            if isinstance(value, (int, float)):
                if float(value).is_integer():
                    return str(int(value))
                return f"{value:.2f}"
            return str(value)

        def format_rupee(value):
            return f"Rs. {format_amount(value)}"

        def y_from_top(page_height, top_value):
            return page_height - top_value

        def draw_summary_card(pdf, x_pos, top_pos, width, height, title, value):
            y_pos = y_from_top(page_height, top_pos + height)

            pdf.setFillColor(colors.HexColor("#3a3a3a"))
            pdf.roundRect(x_pos + 2, y_pos - 2, width, height, 10, stroke=0, fill=1)

            pdf.setFillColor(colors.white)
            pdf.setStrokeColor(colors.black)
            pdf.setLineWidth(1.4)
            pdf.roundRect(x_pos, y_pos, width, height, 10, stroke=1, fill=1)

            pdf.setFillColor(colors.black)
            pdf.setFont("Helvetica-Bold", 10.5)
            pdf.drawCentredString(x_pos + (width / 2), y_pos + height - 21, str(title))

            pdf.setFont("Helvetica-Bold", 12.5)
            pdf.drawCentredString(x_pos + (width / 2), y_pos + 12, str(value))

        try:
            month_label = date.fromisoformat(f"{report_month}-01").strftime("%B, %Y")
        except ValueError:
            month_label = report_month

        shop = get_current_shop()
        shop_name = (shop or {}).get("name", "Daily Ledger")
        generated_on = datetime.now().strftime("%d/%m/%Y %H:%M")

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
        page_width, page_height = A4

        title_color = colors.HexColor("#243f6b")
        muted_color = colors.HexColor("#6f6f6f")
        content_left = 72
        content_right = page_width - 72

        logo_x = 44
        logo_top = 24
        logo_target_width = 110
        logo_target_height = 50
        logo_path = os.path.join(current_app.root_path, "static", "images", "Daily_ledger_widename.svg")
        logo_drawn = False

        if svg2rlg and os.path.exists(logo_path):
            try:
                logo_drawing = svg2rlg(logo_path)
                if logo_drawing and logo_drawing.width and logo_drawing.height:
                    original_width = float(logo_drawing.width)
                    original_height = float(logo_drawing.height)
                    logo_scale = min(
                        logo_target_width / original_width,
                        logo_target_height / original_height,
                    )
                    logo_draw_width = original_width * logo_scale
                    logo_draw_height = original_height * logo_scale
                    logo_drawing.scale(logo_scale, logo_scale)
                    logo_y = y_from_top(page_height, logo_top + logo_draw_height)
                    renderPDF.draw(logo_drawing, pdf, logo_x, logo_y)
                    logo_drawn = True
            except Exception as logo_error:
                current_app.logger.warning(f"Failed to render SVG logo in monthly PDF: {logo_error}")

        if not logo_drawn:
            pdf.setFillColor(colors.HexColor("#8ca0b8"))
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(logo_x, y_from_top(page_height, logo_top + 24), "Daily Ledger")

        pdf.setFillColor(muted_color)
        pdf.setFont("Helvetica", 11)
        pdf.drawRightString(content_right, y_from_top(page_height, 50), f"Generated On: {generated_on}")

        pdf.setFillColor(title_color)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawCentredString(page_width / 2, y_from_top(page_height, 132), "MONTHLY SUMMARY REPORT")

        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(content_left, y_from_top(page_height, 182), month_label)

        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawCentredString(page_width / 2, y_from_top(page_height, 206), shop_name)

        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(content_left, y_from_top(page_height, 238), "Overall Summary")

        card_width = 108
        card_height = 58
        card_gap = 12
        cards_total_width = (card_width * 4) + (card_gap * 3)
        cards_start_x = (page_width - cards_total_width) / 2
        cards_top = 256

        cards = [
            ("Total Credit", format_rupee(report["total_credit"])),
            ("Total Debited", format_rupee(report["total_debit"])),
            ("Closing Bal", format_rupee(report["month_closing_balance"])),
            ("Most used Bank", str(report["most_used_bank"])),
        ]

        for index, (label, value) in enumerate(cards):
            x_pos = cards_start_x + index * (card_width + card_gap)
            draw_summary_card(
                pdf=pdf,
                x_pos=x_pos,
                top_pos=cards_top,
                width=card_width,
                height=card_height,
                title=label,
                value=value,
            )

        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(content_left, y_from_top(page_height, 390), "Bank-Wise Summary")

        table_data = [["Bank Name", "Total credits", "Total debits", "Closing balance"]]
        if bank_wise:
            for bank in bank_wise:
                table_data.append([
                    str(bank["bank"]),
                    format_rupee(bank["total_credit"]),
                    format_rupee(bank["total_debit"]),
                    format_rupee(bank["closing_balance"]),
                ])
        else:
            table_data.append(["-", "Rs. 0", "Rs. 0", "Rs. 0"])

        table_width = page_width - (content_left * 2)
        bank_table = Table(table_data, colWidths=[table_width / 4] * 4, repeatRows=1)
        bank_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f4f4")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, 0), 12),
            ("FONTSIZE", (0, 1), (-1, -1), 11),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TEXTCOLOR", (1, 1), (1, -1), colors.HexColor("#355f2b")),
            ("TEXTCOLOR", (2, 1), (2, -1), colors.HexColor("#d00000")),
        ]))

        table_draw_x = content_left
        table_top = 414
        _, table_height = bank_table.wrap(table_width, 200)
        table_draw_y = y_from_top(page_height, table_top + table_height)
        bank_table.drawOn(pdf, table_draw_x, table_draw_y)

        pdf.showPage()
        pdf.save()
        buffer.seek(0)
        return buffer

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
        start_date = (request.args.get("start_date") or "").strip()
        end_date = (request.args.get("end_date") or "").strip()
        page = request.args.get("page", default=1, type=int) or 1
        if page < 1:
            page = 1
        days_per_page = 15
        total_days = 0
        total_pages = 0
        grouped_entries = []

        if start_date and end_date:
            valid_start, valid_end = normalize_date_range(start_date, end_date)
            if not valid_start:
                flash("Please select a valid date range.", "danger")
            else:
                all_dates = get_day_wise_dates_in_range(valid_start, valid_end)
                total_days = len(all_dates)
                if total_days > 0:
                    total_pages = (total_days + days_per_page - 1) // days_per_page
                    if page > total_pages:
                        page = total_pages
                    start_idx = (page - 1) * days_per_page
                    end_idx = start_idx + days_per_page
                    page_dates = all_dates[start_idx:end_idx]
                    entries = get_daily_entries_for_dates(page_dates)
                    grouped_entries = group_entries_by_date(entries)

        return render_template(
            "daily_report.html",
            grouped_entries=grouped_entries,
            start_date=start_date or None,
            end_date=end_date or None,
            page=page,
            days_per_page=days_per_page,
            total_days=total_days,
            total_pages=total_pages,
        )

    @app.route("/monthly-report")
    def monthly_report():
        report_month = (request.args.get("report_month") or "").strip()
        searched_month = bool(report_month)
        report = None
        bank_wise = []

        if report_month:
            month_start, month_end = get_month_date_range(report_month)
            if not month_start:
                flash("Please select a valid month.", "danger")
            else:
                report, bank_wise = build_report_for_range(month_start, month_end)
                if report:
                    report["month_closing_balance"] = report.pop("closing_balance")

        return render_template(
            "monthly_report.html",
            report=report,
            bank_wise=bank_wise,
            selected_month=report_month,
            searched_month=searched_month,
        )

    @app.route("/monthly-report/pdf")
    def download_monthly_report_pdf():
        report_month = (request.args.get("report_month") or "").strip()

        if not re.fullmatch(r"\d{4}-\d{2}", report_month):
            flash("Please select a valid month before downloading PDF.", "danger")
            return redirect(url_for("monthly_report"))

        month_start, month_end = get_month_date_range(report_month)
        if not month_start:
            flash("Please select a valid month before downloading PDF.", "danger")
            return redirect(url_for("monthly_report"))

        report, bank_wise = build_report_for_range(month_start, month_end)

        if not report:
            flash("No data found for the selected month.", "danger")
            return redirect(url_for("monthly_report", report_month=report_month))
        report["month_closing_balance"] = report.pop("closing_balance")

        try:
            date_obj = date.fromisoformat(f"{report_month}-01")
            formatted_name = f"{date_obj.strftime('%B, %Y')} monthly Report.pdf"
        except ValueError:
            formatted_name = f"monthly_summary_report_{report_month}.pdf"

        pdf_file = build_monthly_pdf(report_month, report, bank_wise)
        return send_file(
            pdf_file,
            as_attachment=True,
            download_name=formatted_name,
            mimetype="application/pdf",
        )

    def build_yearly_pdf(report_year, report, bank_wise):
        def format_amount(value):
            if isinstance(value, (int, float)):
                if float(value).is_integer():
                    return str(int(value))
                return f"{value:.2f}"
            return str(value)

        def format_rupee(value):
            return f"Rs. {format_amount(value)}"

        def y_from_top(page_height, top_value):
            return page_height - top_value

        def draw_summary_card(pdf, x_pos, top_pos, width, height, title, value):
            y_pos = y_from_top(page_height, top_pos + height)

            pdf.setFillColor(colors.HexColor("#3a3a3a"))
            pdf.roundRect(x_pos + 2, y_pos - 2, width, height, 10, stroke=0, fill=1)

            pdf.setFillColor(colors.white)
            pdf.setStrokeColor(colors.black)
            pdf.setLineWidth(1.4)
            pdf.roundRect(x_pos, y_pos, width, height, 10, stroke=1, fill=1)

            pdf.setFillColor(colors.black)
            pdf.setFont("Helvetica-Bold", 10.5)
            pdf.drawCentredString(x_pos + (width / 2), y_pos + height - 21, str(title))

            pdf.setFont("Helvetica-Bold", 12.5)
            pdf.drawCentredString(x_pos + (width / 2), y_pos + 12, str(value))

        shop = get_current_shop()
        shop_name = (shop or {}).get("name", "Daily Ledger")
        generated_on = datetime.now().strftime("%d/%m/%Y %H:%M")

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
        page_width, page_height = A4

        title_color = colors.HexColor("#243f6b")
        muted_color = colors.HexColor("#6f6f6f")
        content_left = 72
        content_right = page_width - 72

        logo_x = 44
        logo_top = 24
        logo_target_width = 110
        logo_target_height = 50
        logo_path = os.path.join(current_app.root_path, "static", "images", "Daily_ledger_widename.svg")
        logo_drawn = False

        if svg2rlg and os.path.exists(logo_path):
            try:
                logo_drawing = svg2rlg(logo_path)
                if logo_drawing and logo_drawing.width and logo_drawing.height:
                    original_width = float(logo_drawing.width)
                    original_height = float(logo_drawing.height)
                    logo_scale = min(
                        logo_target_width / original_width,
                        logo_target_height / original_height,
                    )
                    logo_draw_width = original_width * logo_scale
                    logo_draw_height = original_height * logo_scale
                    logo_drawing.scale(logo_scale, logo_scale)
                    logo_y = y_from_top(page_height, logo_top + logo_draw_height)
                    renderPDF.draw(logo_drawing, pdf, logo_x, logo_y)
                    logo_drawn = True
            except Exception as logo_error:
                current_app.logger.warning(f"Failed to render SVG logo in yearly PDF: {logo_error}")

        if not logo_drawn:
            pdf.setFillColor(colors.HexColor("#8ca0b8"))
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(logo_x, y_from_top(page_height, logo_top + 24), "Daily Ledger")

        pdf.setFillColor(muted_color)
        pdf.setFont("Helvetica", 11)
        pdf.drawRightString(content_right, y_from_top(page_height, 50), f"Generated On: {generated_on}")

        pdf.setFillColor(title_color)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawCentredString(page_width / 2, y_from_top(page_height, 132), "YEARLY SUMMARY REPORT")

        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(content_left, y_from_top(page_height, 182), f"Year: {report_year}")

        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawCentredString(page_width / 2, y_from_top(page_height, 206), shop_name)

        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(content_left, y_from_top(page_height, 238), "Overall Summary")

        card_width = 108
        card_height = 58
        card_gap = 12
        cards_total_width = (card_width * 4) + (card_gap * 3)
        cards_start_x = (page_width - cards_total_width) / 2
        cards_top = 256

        cards = [
            ("Total Credit", format_rupee(report["total_credit"])),
            ("Total Debited", format_rupee(report["total_debit"])),
            ("Closing Bal", format_rupee(report["year_closing_balance"])),
            ("Most used Bank", str(report["most_used_bank"])),
        ]

        for index, (label, value) in enumerate(cards):
            x_pos = cards_start_x + index * (card_width + card_gap)
            draw_summary_card(
                pdf=pdf,
                x_pos=x_pos,
                top_pos=cards_top,
                width=card_width,
                height=card_height,
                title=label,
                value=value,
            )

        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(content_left, y_from_top(page_height, 390), "Bank-Wise Summary")

        table_data = [["Bank Name", "Total credits", "Total debits", "Closing balance"]]
        if bank_wise:
            for bank in bank_wise:
                table_data.append([
                    str(bank["bank"]),
                    format_rupee(bank["total_credit"]),
                    format_rupee(bank["total_debit"]),
                    format_rupee(bank["closing_balance"]),
                ])
        else:
            table_data.append(["-", "Rs. 0", "Rs. 0", "Rs. 0"])

        table_width = page_width - (content_left * 2)
        bank_table = Table(table_data, colWidths=[table_width / 4] * 4, repeatRows=1)
        bank_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f4f4f4")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, 0), 12),
            ("FONTSIZE", (0, 1), (-1, -1), 11),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TEXTCOLOR", (1, 1), (1, -1), colors.HexColor("#355f2b")),
            ("TEXTCOLOR", (2, 1), (2, -1), colors.HexColor("#d00000")),
        ]))

        table_draw_x = content_left
        table_top = 414
        _, table_height = bank_table.wrap(table_width, 200)
        table_draw_y = y_from_top(page_height, table_top + table_height)
        bank_table.drawOn(pdf, table_draw_x, table_draw_y)

        pdf.showPage()
        pdf.save()
        buffer.seek(0)
        return buffer

    @app.route("/yearly-report/pdf")
    def download_yearly_report_pdf():
        report_year = (request.args.get("report_year") or "").strip()

        if not re.fullmatch(r"\d{4}", report_year):
            flash("Please select a valid year before downloading PDF.", "danger")
            return redirect(url_for("yearly_report"))

        year_start, year_end = get_year_date_range(report_year)
        if not year_start:
            flash("Please select a valid year before downloading PDF.", "danger")
            return redirect(url_for("yearly_report"))

        report, bank_wise = build_report_for_range(year_start, year_end)
        if not report:
            flash("No data found for the selected year.", "danger")
            return redirect(url_for("yearly_report", report_year=report_year))
        report["year_closing_balance"] = report.pop("closing_balance")

        formatted_name = f"{report_year} Yearly Summary Report.pdf"
        pdf_file = build_yearly_pdf(report_year, report, bank_wise)
        
        return send_file(
            pdf_file,
            as_attachment=True,
            download_name=formatted_name,
            mimetype="application/pdf",
        )

    @app.route("/yearly-report")
    def yearly_report():
        report_year = (request.args.get("report_year") or "").strip()
        selected_year = report_year or str(date.today().year)
        searched_year = bool(report_year)
        report = None
        bank_wise = []

        if report_year:
            year_start, year_end = get_year_date_range(report_year)
            if not year_start:
                flash("Please select a valid year.", "danger")
            else:
                report, bank_wise = build_report_for_range(year_start, year_end)
                if report:
                    report["year_closing_balance"] = report.pop("closing_balance")

        return render_template(
            "yearly_report.html",
            report=report,
            bank_wise=bank_wise,
            selected_year=selected_year,
            searched_year=searched_year,
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
            range_start, range_end = get_month_date_range(period_value)
            if not range_start:
                flash("Please select a valid month.", "danger")
                return redirect(url_for("reports"))
            period_label = f"Month {period_value}"
            recalc_start_date = range_start
        else:
            range_start, range_end = get_year_date_range(period_value)
            if not range_start:
                flash("Please select a valid year.", "danger")
                return redirect(url_for("reports"))
            period_label = f"Year {period_value}"
            recalc_start_date = range_start

        query = {
            "date": {"$gte": range_start, "$lte": range_end},
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

        report, bank_wise = build_report_for_range(start_date, end_date)
        if report:
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
        start_date = (request.args.get("start_date") or "").strip()
        end_date = (request.args.get("end_date") or "").strip()
        report = None
        bank_wise = []

        if start_date and end_date:
            valid_start, valid_end = normalize_date_range(start_date, end_date)
            if not valid_start:
                flash("Please select a valid date range.", "danger")
            else:
                report, bank_wise = build_report_for_range(valid_start, valid_end)
                if report:
                    report["range_closing_balance"] = report.pop("closing_balance")

        return render_template(
            "custom_report.html",
            report=report,
            bank_wise=bank_wise,
            start_date=start_date or None,
            end_date=end_date or None,
        )
