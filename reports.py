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
    # Allowed year range for yearly report inputs.
    report_year_min = 2000
    report_year_max = 2100
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
            if year_int < report_year_min or year_int > report_year_max:
                return None, None
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
            # Fallback to in-memory summary if aggregation fails.
            current_app.logger.error(f"Database error while aggregating report range: {e}")
            entries = get_summary_entries_in_range(start_date, end_date)
            if not entries:
                return None, []
            return build_report(entries)

    def attach_closing_balance(report, target_key):
        # Normalize the common closing key for each report type/template.
        if report and "closing_balance" in report:
            report[target_key] = report.pop("closing_balance")
        return report

    def build_report_for_range_with_closing_key(start_date, end_date, closing_key):
        report, bank_wise = build_report_for_range(start_date, end_date)
        attach_closing_balance(report, closing_key)
        return report, bank_wise

    def resolve_month_range_or_flash(report_month, error_message):
        # Shared month-range validator for routes with consistent flash behavior.
        month_start, month_end = get_month_date_range(report_month)
        if not month_start:
            flash(error_message, "danger")
            return None, None
        return month_start, month_end

    def resolve_year_range_or_flash(report_year, error_message):
        # Shared year-range validator for routes with consistent flash behavior.
        year_start, year_end = get_year_date_range(report_year)
        if not year_start:
            flash(error_message, "danger")
            return None, None
        return year_start, year_end

    def delete_entries_and_recalculate(range_start, range_end, recalc_start_date):
        # Keep balances consistent by recalculating only affected banks after delete.
        query = {
            "date": {"$gte": range_start, "$lte": range_end},
            "shop_identifier": current_shop_identifier(),
        }
        affected_bank_ids = entries_col.distinct("bank_id", query)
        delete_result = entries_col.delete_many(query)

        for bank_id in affected_bank_ids:
            try:
                recalculate_bank_balances_from_date(bank_id, recalc_start_date)
            except Exception as recalc_error:
                current_app.logger.error(
                    f"Balance recalc failed for bank_id={bank_id}: {recalc_error}"
                )

        return delete_result.deleted_count

    def format_amount_for_pdf(value):
        # Format numbers in Indian grouping (e.g., 1,23,456.78) for PDF display.
        try:
            amount = float(value)
        except (TypeError, ValueError):
            return "0.00"

        is_negative = amount < 0
        amount = abs(amount)
        rounded_amount = round(amount + 1e-9, 2)

        whole_part = int(rounded_amount)
        decimal_part = int(round((rounded_amount - whole_part) * 100))
        if decimal_part == 100:
            whole_part += 1
            decimal_part = 0

        whole_text = str(whole_part)
        if len(whole_text) > 3:
            last_three = whole_text[-3:]
            rest = whole_text[:-3]
            parts = []
            while len(rest) > 2:
                parts.insert(0, rest[-2:])
                rest = rest[:-2]
            if rest:
                parts.insert(0, rest)
            whole_text = ",".join(parts + [last_three])

        formatted = f"{whole_text}.{decimal_part:02d}"
        return f"-{formatted}" if is_negative else formatted

    def format_rupee_for_pdf(value):
        return f"Rs. {format_amount_for_pdf(value)}"

    def build_summary_pdf(period_value, report, bank_wise, report_title, period_label, closing_balance_key, period_kind):
        # Shared PDF renderer used by both monthly and yearly exports.
        def y_from_top(top_value):
            return page_height - top_value

        def draw_summary_card(x_pos, top_pos, width, height, title, value):
            # Card block for the top summary metrics row.
            y_pos = y_from_top(top_pos + height)
            pdf.setFillColor(colors.white)
            pdf.setStrokeColor(card_border_color)
            pdf.setLineWidth(max(0.8, scaled(1)))
            pdf.roundRect(x_pos, y_pos, width, height, card_radius, stroke=1, fill=1)

            pdf.setFillColor(label_color)
            pdf.setFont("Helvetica-Bold", card_label_font)
            pdf.drawString(x_pos + scaled(8), y_pos + height - scaled(18), str(title).upper())

            pdf.setFillColor(text_dark)
            pdf.setFont("Helvetica-Bold", card_value_font)
            pdf.drawString(x_pos + scaled(8), y_pos + scaled(16), str(value))

        shop = get_current_shop()
        shop_name = (shop or {}).get("name", "Daily Ledger")
        generated_on = datetime.now().strftime("%d/%m/%Y %H:%M")

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
        page_width, page_height = A4

        page_bg_color = colors.white
        border_color = colors.HexColor("#e2e8f0")
        card_border_color = colors.HexColor("#d7dee8")
        text_dark = colors.HexColor("#1e293b")
        label_color = colors.HexColor("#7b8ca4")
        muted_text = colors.HexColor("#64748b")

        pdf.setFillColor(page_bg_color)
        pdf.rect(0, 0, page_width, page_height, stroke=0, fill=1)

        normal_margin = 56.69  # 2 cm in points
        left_margin = normal_margin
        right_margin = normal_margin
        top_margin = normal_margin
        bottom_margin = normal_margin
        base_top_margin = 16
        top_offset = top_margin - base_top_margin
        content_width = page_width - left_margin - right_margin
        layout_scale = max(0.9, min(1.0, content_width / 510.24))

        def scaled(value):
            return value * layout_scale

        card_radius = max(8, scaled(10))
        card_label_font = max(7.0, scaled(7.5))
        card_value_font = max(10.5, scaled(12))
        section_heading_font = max(14.0, scaled(16))
        table_header_font = max(8.0, scaled(8.5))
        table_body_font = max(9.0, scaled(10))
        table_side_padding = max(8, scaled(12))
        table_vertical_padding = max(7, scaled(10))
        compact_header_font = max(7.2, scaled(7.6))
        compact_body_font = max(8.2, scaled(9))
        compact_vertical_padding = max(5.5, scaled(7))

        def from_content_top(top_value):
            return top_value + top_offset

        logo_x = left_margin
        logo_top = from_content_top(14)
        logo_target_width = max(112, scaled(128))
        logo_target_height = max(30, scaled(34))
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
                    logo_draw_height = original_height * logo_scale
                    logo_drawing.scale(logo_scale, logo_scale)
                    logo_y = y_from_top(logo_top + logo_draw_height)
                    renderPDF.draw(logo_drawing, pdf, logo_x, logo_y)
                    logo_drawn = True
            except Exception as logo_error:
                current_app.logger.warning(f"Failed to render SVG logo in {period_kind} PDF: {logo_error}")

        if not logo_drawn:
            pdf.setFillColor(text_dark)
            pdf.setFont("Helvetica-Bold", max(11, scaled(12)))
            pdf.drawString(logo_x, y_from_top(from_content_top(31)), "Daily Ledger")

        pdf.setFillColor(muted_text)
        pdf.setFont("Helvetica", max(8.5, scaled(9.5)))
        pdf.drawRightString(page_width - right_margin, y_from_top(from_content_top(31)), f"Generated On: {generated_on}")

        pdf.setFillColor(text_dark)
        pdf.setFont("Helvetica-Bold", max(19, scaled(22)))
        pdf.drawCentredString(left_margin + (content_width / 2), y_from_top(from_content_top(68)), report_title)

        info_label_x = left_margin + 6
        info_value_x = left_margin + max(88, min(104, content_width * 0.22))

        pdf.setFillColor(text_dark)
        pdf.setFont("Helvetica-Bold", max(10, scaled(11.5)))
        pdf.drawString(info_label_x, y_from_top(from_content_top(98)), period_label)
        pdf.drawString(info_label_x, y_from_top(from_content_top(114)), "Shop Name:")

        pdf.setFont("Helvetica-Bold", max(9.5, scaled(10.5)))
        pdf.drawString(info_value_x, y_from_top(from_content_top(98)), str(period_value))
        pdf.drawString(info_value_x, y_from_top(from_content_top(114)), str(shop_name))

        overall_heading_top = from_content_top(154)
        overall_bullet_x = left_margin + 6
        overall_bullet_y = y_from_top(overall_heading_top + 7)
        pdf.setFillColor(colors.black)
        pdf.circle(overall_bullet_x, overall_bullet_y, max(2.8, scaled(3.2)), stroke=0, fill=1)

        pdf.setFillColor(text_dark)
        pdf.setFont("Helvetica-Bold", section_heading_font)
        pdf.drawString(left_margin + 16, y_from_top(overall_heading_top + 10), "Overall Summary")

        cards_top = from_content_top(178)
        card_gap = max(6, scaled(8))
        card_width = (content_width - (card_gap * 3)) / 4
        card_height = max(72, scaled(78))

        cards = [
            ("Total Credit", format_rupee_for_pdf(report.get("total_credit", 0))),
            ("Total Debited", format_rupee_for_pdf(report.get("total_debit", 0))),
            ("Closing Bal", format_rupee_for_pdf(report.get(closing_balance_key, 0))),
            ("Most used Bank", str(report.get("most_used_bank", "-"))),
        ]

        for index, (label, value) in enumerate(cards):
            draw_summary_card(
                x_pos=left_margin + (index * (card_width + card_gap)),
                top_pos=cards_top,
                width=card_width,
                height=card_height,
                title=label,
                value=value,
            )

        bank_heading_top = cards_top + card_height + max(24, scaled(28))
        bank_bullet_x = left_margin + 6
        bank_bullet_y = y_from_top(bank_heading_top + 7)
        pdf.setFillColor(colors.black)
        pdf.circle(bank_bullet_x, bank_bullet_y, max(2.8, scaled(3.2)), stroke=0, fill=1)

        pdf.setFillColor(text_dark)
        pdf.setFont("Helvetica-Bold", section_heading_font)
        pdf.drawString(left_margin + 16, y_from_top(bank_heading_top + 10), "Bank-Wise Summary")

        table_data = [["BANK NAME", "TOTAL CREDITS", "TOTAL DEBITS", "CLOSING BALANCE"]]
        if bank_wise:
            for bank in bank_wise:
                table_data.append([
                    str(bank.get("bank", "-")),
                    format_amount_for_pdf(bank.get("total_credit", 0)),
                    format_amount_for_pdf(bank.get("total_debit", 0)),
                    format_amount_for_pdf(bank.get("closing_balance", 0)),
                ])
        else:
            table_data.append(["-", "0.00", "0.00", "0.00"])

        table_data.append([
            "Grand Total",
            format_rupee_for_pdf(report.get("total_credit", 0)),
            format_rupee_for_pdf(report.get("total_debit", 0)),
            format_rupee_for_pdf(report.get(closing_balance_key, 0)),
        ])

        table_width = content_width
        bank_table = Table(
            table_data,
            colWidths=[table_width * 0.34, table_width * 0.22, table_width * 0.22, table_width * 0.22],
            repeatRows=1,
        )

        grand_row_index = len(table_data) - 1
        bank_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("TEXTCOLOR", (0, 0), (-1, 0), label_color),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), table_header_font),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 1), (-1, -1), table_body_font),
            ("TEXTCOLOR", (0, 1), (-1, -1), text_dark),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEABOVE", (0, 0), (-1, 0), 1, border_color),
            ("LINEBELOW", (0, 0), (-1, 0), 1, border_color),
            ("LINEBELOW", (0, 1), (-1, -1), 0.7, border_color),
            ("LEFTPADDING", (0, 0), (-1, -1), table_side_padding),
            ("RIGHTPADDING", (0, 0), (-1, -1), table_side_padding),
            ("TOPPADDING", (0, 0), (-1, -1), table_vertical_padding),
            ("BOTTOMPADDING", (0, 0), (-1, -1), table_vertical_padding),
            ("BACKGROUND", (0, grand_row_index), (-1, grand_row_index), colors.HexColor("#f8fafc")),
            ("FONTNAME", (0, grand_row_index), (-1, grand_row_index), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, grand_row_index), (-1, grand_row_index), text_dark),
            ("LINEABOVE", (0, grand_row_index), (-1, grand_row_index), 1, border_color),
            ("LINEBELOW", (0, grand_row_index), (-1, grand_row_index), 1, border_color),
            ("BOX", (0, 0), (-1, -1), 1, border_color),
        ]))

        table_top = bank_heading_top + max(24, scaled(28))
        _, table_height = bank_table.wrap(table_width, page_height)
        table_y = y_from_top(table_top + table_height)

        if table_y < bottom_margin:
            # Compact table fonts/padding if content is close to bottom margin.
            bank_table.setStyle(TableStyle([
                ("FONTSIZE", (0, 0), (-1, 0), compact_header_font),
                ("FONTSIZE", (0, 1), (-1, -1), compact_body_font),
                ("TOPPADDING", (0, 0), (-1, -1), compact_vertical_padding),
                ("BOTTOMPADDING", (0, 0), (-1, -1), compact_vertical_padding),
            ]))
            _, table_height = bank_table.wrap(table_width, page_height)
            table_y = y_from_top(table_top + table_height)
            if table_y < bottom_margin:
                table_y = bottom_margin

        bank_table.drawOn(pdf, left_margin, table_y)

        pdf.showPage()
        pdf.save()
        buffer.seek(0)
        return buffer

    def build_monthly_pdf(report_month, report, bank_wise):
        try:
            period_value = date.fromisoformat(f"{report_month}-01").strftime("%B %Y")
        except ValueError:
            period_value = report_month

        return build_summary_pdf(
            period_value=period_value,
            report=report,
            bank_wise=bank_wise,
            report_title="MONTHLY SUMMARY REPORT",
            period_label="Report Month:",
            closing_balance_key="month_closing_balance",
            period_kind="monthly",
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
            month_start, month_end = resolve_month_range_or_flash(
                report_month,
                "Please select a valid month.",
            )
            if month_start:
                report, bank_wise = build_report_for_range_with_closing_key(
                    month_start,
                    month_end,
                    "month_closing_balance",
                )

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

        month_start, month_end = resolve_month_range_or_flash(
            report_month,
            "Please select a valid month before downloading PDF.",
        )
        if not month_start:
            return redirect(url_for("monthly_report"))

        report, bank_wise = build_report_for_range_with_closing_key(
            month_start,
            month_end,
            "month_closing_balance",
        )

        if not report:
            flash("No data found for the selected month.", "danger")
            return redirect(url_for("monthly_report", report_month=report_month))

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
        return build_summary_pdf(
            period_value=str(report_year),
            report=report,
            bank_wise=bank_wise,
            report_title="YEARLY SUMMARY REPORT",
            period_label="Report Year:",
            closing_balance_key="year_closing_balance",
            period_kind="yearly",
        )

    @app.route("/yearly-report/pdf")
    def download_yearly_report_pdf():
        report_year = (request.args.get("report_year") or "").strip()

        year_start, year_end = resolve_year_range_or_flash(
            report_year,
            "Please select a valid year before downloading PDF.",
        )
        if not year_start:
            return redirect(url_for("yearly_report"))

        report, bank_wise = build_report_for_range_with_closing_key(
            year_start,
            year_end,
            "year_closing_balance",
        )
        if not report:
            flash("No data found for the selected year.", "danger")
            return redirect(url_for("yearly_report", report_year=report_year))

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
            year_start, year_end = resolve_year_range_or_flash(
                report_year,
                "Please select a valid year.",
            )
            if year_start:
                report, bank_wise = build_report_for_range_with_closing_key(
                    year_start,
                    year_end,
                    "year_closing_balance",
                )

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

        if delete_type == "month":
            range_start, range_end = resolve_month_range_or_flash(
                period_value,
                "Please select a valid month.",
            )
            if not range_start:
                return redirect(url_for("reports"))
            period_label = f"Month {period_value}"
        else:
            range_start, range_end = resolve_year_range_or_flash(
                period_value,
                f"Please select a valid year between {report_year_min} and {report_year_max}.",
            )
            if not range_start:
                return redirect(url_for("reports"))
            period_label = f"Year {period_value}"

        shop = get_current_shop()
        if not shop:
            flash("Unable to verify account. Please log in again.", "danger")
            return redirect(url_for("login"))

        if not password or not check_password_hash_fn(shop.get("password_hash", ""), password):
            flash("Incorrect password. Data was not deleted.", "danger")
            return redirect(url_for("reports"))

        try:
            deleted_count = delete_entries_and_recalculate(
                range_start=range_start,
                range_end=range_end,
                recalc_start_date=range_start,
            )

            flash(
                f"{period_label} data deleted successfully. {deleted_count} entries removed.",
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

        report, bank_wise = build_report_for_range_with_closing_key(
            start_date,
            end_date,
            "week_closing_balance",
        )

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
                report, bank_wise = build_report_for_range_with_closing_key(
                    valid_start,
                    valid_end,
                    "range_closing_balance",
                )

        return render_template(
            "custom_report.html",
            report=report,
            bank_wise=bank_wise,
            start_date=start_date or None,
            end_date=end_date or None,
        )
