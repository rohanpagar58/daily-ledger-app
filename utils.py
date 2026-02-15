from datetime import datetime


def group_entries_by_date(entries):
    grouped_entries = []
    current_date = None
    for entry in entries:
        entry_date = entry.get("date")
        if entry_date != current_date:
            grouped_entries.append({"date": entry_date, "rows": [entry]})
            current_date = entry_date
        else:
            grouped_entries[-1]["rows"].append(entry)
    return grouped_entries


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
