import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
import telebot
from telebot import types
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import schedule
from openai import OpenAI

# Load .env so OPENAI_API_KEY (and TELEGRAM_BOT_TOKEN) work without exporting in shell
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =======================
# CONFIGURATION
# =======================
BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8790848824:AAHAYFn15nWp2bPWHmf9iV6rdDmpq_VyKY8",
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set! AI features disabled.")
    openai_client = None
else:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Telegram numeric user IDs for admins (you + business partner)
# TODO: Update these when swapping to a new bot
ADMIN_IDS = [1253972975, 515525969]

# Level-specific task library channels (forward tasks from here by level)
# TODO: Update these when swapping to a new bot (new bot will have different channel IDs)
LEVEL_CHANNELS = {
    "A1": -1003853572928,
    "A2": -1003790553224,
    "B1": -1003750480222,
    "B2": -1003530416415,
}

# Your DM with the bot (receive student voice replies + transcript/draft here)
# TODO: Update this when swapping to a new bot
ADMIN_FEEDBACK_CHAT_ID = 1253972975

# Work chat group (you + partner); student voice results are also sent here (bot must be in the group)
# TODO: Update this when swapping to a new bot
WORK_CHAT_ID = -5158365422
# Partner's DM (so they get practice/test results even if work chat fails)
# TODO: Update this when swapping to a new bot
PARTNER_CHAT_ID = 515525969

# Google Sheets configuration (path relative to this script so it works from any cwd)
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
GOOGLE_SERVICE_ACCOUNT_JSON = os.path.join(_BOT_DIR, "service_account.json")
SPREADSHEET_NAME = "SpeakUp!"
STUDENTS_SHEET_NAME = "Students"
# Real "Students" tab columns A1–K1: Name, Telegram Handle, Chat ID, Telephone, Student ID, Level, Tier End Date, Tasks Sent This Week, Tasks Due Today, Balance Due, Notes
STUDENTS_HEADERS = ["Name", "Telegram Handle", "Chat ID", "Telephone", "Student ID", "Level", "Tier End Date", "Tasks Sent This Week", "Tasks Due Today", "Balance Due", "Notes"]
LEVELS = ["A1", "A2", "B1", "B2"]
REGISTRATION_STATE_PATH = os.path.join(_BOT_DIR, "registration_state.json")
# Task list tabs: "A2 Task List", "B2 Task List" (etc.) with columns "Task #", "Message ID" (channel), "Script Text"
# Task Log tab: Date Sent, Student, Task #, Voice File Name, Reply Received, Week #, Level (Total Tasks Sent in Students is formula from this)
TASK_LOG_SHEET_NAME = "Task Log"
SETTINGS_SHEET_NAME = "Settings"
SETTINGS_HEADERS = ["Chat ID", "Language", "Notifications", "Referral Code", "Referral Count", "Status", "Last Activity"]
TASK_LOG_HEADERS = [
    "Date Sent", "Student", "Task #", "Voice File Name", "Reply Received", "Week #", "Level",
    "Chat ID", "Transcript", "Pronunciation", "Grammar", "Vocabulary", "Fluency",
]

# On Railway/Heroku: no file on disk. Set env var GOOGLE_CREDENTIALS_JSON to the full JSON
# content of your service account key; we write it to service_account.json at startup.
_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if _creds_json:
    with open(GOOGLE_SERVICE_ACCOUNT_JSON, "w") as f:
        f.write(_creds_json)
elif not os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
    print("ERROR: service_account.json not found and GOOGLE_CREDENTIALS_JSON not set. Set GOOGLE_CREDENTIALS_JSON on Railway to the full JSON key.")

# =======================
# GOOGLE SHEETS SETUP
# =======================
def get_gspread_client():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        GOOGLE_SERVICE_ACCOUNT_JSON, scope
    )
    client = gspread.authorize(creds)
    return client

def get_students_worksheet():
    client = get_gspread_client()
    spreadsheet = client.open(SPREADSHEET_NAME)
    try:
        ws = spreadsheet.worksheet(STUDENTS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=STUDENTS_SHEET_NAME, rows="1000", cols=len(STUDENTS_HEADERS))
        ws.append_row(STUDENTS_HEADERS)
    return ws

def register_student(
    chat_id: int,
    level: str,
    name: str = "",
    telegram_handle: str = "",
    telephone: str = "",
):
    ws = get_students_worksheet()
    all_rows = ws.get_all_records()
    row_index_to_update = None
    for idx, row in enumerate(all_rows, start=2):
        if str(row.get("Chat ID")) == str(chat_id):
            row_index_to_update = idx
            break
    if row_index_to_update:
        ws.update_cell(row_index_to_update, 6, level)   # Level = column F
        if telephone:
            ws.update_cell(row_index_to_update, 4, telephone)  # Telephone = column D
    else:
        ws.append_row([
            name or "",
            telegram_handle or "",
            str(chat_id),
            telephone or "",
            "", level, "", "", "", "", "",
        ])

def get_students_by_level(level: str) -> List[int]:
    ws = get_students_worksheet()
    all_rows = ws.get_all_records()
    chat_ids: List[int] = []
    for row in all_rows:
        if str(row.get("Level", "")).strip().lower() == level.strip().lower():
            try:
                chat_ids.append(int(row.get("Chat ID")))
            except (TypeError, ValueError):
                continue
    return chat_ids


def get_student_row(chat_id: int) -> Optional[dict]:
    """Get the Students row for this chat_id, or None."""
    ws = get_students_worksheet()
    for row in ws.get_all_records():
        if str(row.get("Chat ID")) == str(chat_id):
            return row
    return None


def get_student_level_and_total_tasks(chat_id: int) -> Tuple[str, int]:
    """Get (level, total_tasks_sent) from Students. Total Tasks Sent is from your formula (count in Task Log)."""
    row = get_student_row(chat_id)
    if not row:
        return "Unknown", 0
    level = str(row.get("Level", "Unknown") or "Unknown").strip()
    try:
        total = int(row.get("Total Tasks Sent") or row.get("Total tasks sent") or 0)
    except (TypeError, ValueError):
        total = 0
    return level, total


def get_student_name(chat_id: int) -> str:
    """Get student name from Students sheet for Task Log."""
    row = get_student_row(chat_id)
    if not row:
        return str(chat_id)
    return str(row.get("Name", "") or row.get("Student", "") or chat_id).strip() or str(chat_id)


def get_task_log_worksheet():
    client = get_gspread_client()
    spreadsheet = client.open(SPREADSHEET_NAME)
    try:
        ws = spreadsheet.worksheet(TASK_LOG_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=TASK_LOG_SHEET_NAME, rows="2000", cols=len(TASK_LOG_HEADERS))
        ws.append_row(TASK_LOG_HEADERS)
    return ws


MOSCOW_TZ = timezone(timedelta(hours=3))

def _now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ)

def append_task_log(student_name: str, task_number: int, level: str, voice_file_name: str = "", chat_id: int = 0):
    """Append a row to Task Log so your Total Tasks Sent formula in Students updates."""
    ws = get_task_log_worksheet()
    now = _now_moscow()
    date_sent = now.strftime("%Y-%m-%d %H:%M")
    week_num = now.isocalendar()[1]
    ws.append_row([
        date_sent, student_name, task_number, voice_file_name or "", "", week_num, level,
        str(chat_id) if chat_id else "", "", "", "", "", "",
    ])


def update_task_log_reply(chat_id: int, transcript: str, scores: dict):
    """Find the most recent Task Log row for this chat_id with no Reply Received and fill it in."""
    ws = get_task_log_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        return
    headers = all_values[0]
    try:
        chat_id_col = headers.index("Chat ID")
        reply_col = headers.index("Reply Received")
        transcript_col = headers.index("Transcript")
        pron_col = headers.index("Pronunciation")
        gram_col = headers.index("Grammar")
        vocab_col = headers.index("Vocabulary")
        fluency_col = headers.index("Fluency")
    except ValueError:
        print("update_task_log_reply: required column not found in Task Log headers")
        return

    # Walk rows in reverse to find the most recent unanswered row for this student
    target_row_idx = None
    for i in range(len(all_values) - 1, 0, -1):
        row = all_values[i]
        row_chat_id = row[chat_id_col] if chat_id_col < len(row) else ""
        row_reply = row[reply_col] if reply_col < len(row) else ""
        if str(row_chat_id).strip() == str(chat_id) and not str(row_reply).strip():
            target_row_idx = i + 1  # gspread rows are 1-indexed
            break

    if target_row_idx is None:
        print(f"update_task_log_reply: no open row found for chat_id={chat_id}")
        return

    now = _now_moscow().strftime("%Y-%m-%d %H:%M")

    # Pronunciation is left blank — filled in manually by the teacher after listening
    updates = [
        {"range": gspread.utils.rowcol_to_a1(target_row_idx, reply_col + 1), "values": [[now]]},
        {"range": gspread.utils.rowcol_to_a1(target_row_idx, transcript_col + 1), "values": [[transcript]]},
        {"range": gspread.utils.rowcol_to_a1(target_row_idx, gram_col + 1), "values": [[scores.get("grammar", "")]]},
        {"range": gspread.utils.rowcol_to_a1(target_row_idx, vocab_col + 1), "values": [[scores.get("vocabulary", "")]]},
        {"range": gspread.utils.rowcol_to_a1(target_row_idx, fluency_col + 1), "values": [[scores.get("fluency", "")]]},
    ]
    try:
        ws.batch_update(updates)
    except Exception as e:
        print(f"update_task_log_reply batch_update error: {e}")


def get_student_task_log(chat_id: int) -> List[dict]:
    """Return all Task Log rows for this chat_id as a list of dicts.

    Matches on Chat ID if available (new tasks), falls back to Student name
    matching for legacy rows without Chat ID.
    """
    ws = get_task_log_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        return []
    headers = all_values[0]
    rows = []

    # Get the student's name for fallback matching
    student_name = get_student_name(chat_id)
    chat_id_str = str(chat_id)

    try:
        chat_id_col = headers.index("Chat ID")
        student_col = headers.index("Student")
    except ValueError:
        return []

    for row in all_values[1:]:
        # First try Chat ID match (new rows)
        if chat_id_col < len(row) and str(row[chat_id_col]).strip() == chat_id_str:
            rows.append(dict(zip(headers, row)))
            continue

        # Fallback: name match for legacy rows
        if student_col < len(row) and str(row[student_col]).strip().lower() == student_name.lower():
            rows.append(dict(zip(headers, row)))

    return rows


def _def_scores(rows: List[dict], key: str) -> List[float]:
    """Extract non-zero numeric scores from task log rows for a given skill key."""
    out = []
    for r in rows:
        try:
            v = int(r.get(key) or 0)
            if v > 0:
                out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _week_streak(rows: List[dict]) -> int:
    """Count how many consecutive ISO weeks (ending with this week) the student replied in."""
    replied_weeks = set()
    for r in rows:
        if r.get("Reply Received"):
            try:
                dt = datetime.strptime(r["Reply Received"][:16], "%Y-%m-%d %H:%M")
                replied_weeks.add(dt.isocalendar()[:2])  # (year, week)
            except (ValueError, KeyError):
                continue
    if not replied_weeks:
        return 0
    now = _now_moscow()
    streak = 0
    year, week, _ = now.isocalendar()
    while (year, week) in replied_weeks:
        streak += 1
        # step back one ISO week
        prev = datetime.fromisocalendar(year, week, 1) - timedelta(weeks=1)
        year, week, _ = prev.isocalendar()
    return streak


def _def_score_bar(value: float, max_val: int = 5) -> str:
    """Turn a 1-5 score into a compact filled/empty bar string."""
    filled = round(value)
    return "█" * filled + "░" * (max_val - filled)


def _def_skill_label(avg: float) -> str:
    if avg == 0:
        return "no data yet"
    if avg >= 4.5:
        return "excellent"
    if avg >= 3.5:
        return "good"
    if avg >= 2.5:
        return "developing"
    return "needs focus"


def _def_format_stats(chat_id: int, period: str = "all") -> Tuple[str, types.InlineKeyboardMarkup]:
    """Build concise stats message with time period selection."""
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "en")

    rows = get_student_task_log(chat_id)
    if not rows:
        if lang == "ru":
            return "История заданий не найдена. Выполните первое задание, чтобы начать отслеживать статистику!", None
        else:
            return "No task history found yet. Complete your first task to start tracking statistics!", None

    # Filter by time period
    now = _now_moscow()
    filtered_rows = []
    if period == "week":
        week_start = now - timedelta(days=now.weekday())  # Monday of current week
        filtered_rows = [r for r in rows if r.get("Date Sent") and datetime.strptime(r["Date Sent"][:10], "%Y-%m-%d") >= week_start]
    elif period == "month":
        month_start = now.replace(day=1)
        filtered_rows = [r for r in rows if r.get("Date Sent") and datetime.strptime(r["Date Sent"][:10], "%Y-%m-%d") >= month_start]
    else:  # all time
        filtered_rows = rows

    total_sent = len(filtered_rows)
    replied_rows = [r for r in filtered_rows if r.get("Reply Received")]
    total_replied = len(replied_rows)
    reply_rate = round(total_replied / total_sent * 100) if total_sent else 0

    level, _ = get_student_level_and_total_tasks(chat_id)

    # Quick skill averages from replied tasks
    skills = ["Pronunciation", "Grammar", "Vocabulary", "Fluency"]
    averages = {}
    for skill in skills:
        vals = _def_scores(replied_rows, skill)
        averages[skill] = round(sum(vals) / len(vals), 1) if vals else 0.0

    scored_skills = {k: v for k, v in averages.items() if v > 0}

    # Create inline keyboard for period selection
    markup = types.InlineKeyboardMarkup(row_width=3)
    period_buttons = []
    periods = [("all", "All Time"), ("month", "This Month"), ("week", "This Week")]
    period_labels = {
        "all": ("All Time", "Все время"),
        "month": ("This Month", "Этот месяц"),
        "week": ("This Week", "Эта неделя")
    }

    for p, _ in periods:
        label = period_labels[p][1] if lang == "ru" else period_labels[p][0]
        period_buttons.append(types.InlineKeyboardButton(
            f"{'✓ ' if p == period else ''}{label}",
            callback_data=f"stats_{p}"
        ))
    markup.add(*period_buttons)

    # Build message
    if lang == "ru":
        period_names = {"all": "за всё время", "month": "этот месяц", "week": "эту неделю"}
        period_name = period_names.get(period, "весь период")

        lines = [
            f"<b>📊 Статистика ({period_name})</b>",
            "",
            f"Уровень: <b>{level}</b>",
            f"Заданий: <b>{total_sent}</b> | Ответов: <b>{total_replied}</b> ({reply_rate}%)",
        ]

        if scored_skills:
            lines.append("")
            lines.append("<b>Средние оценки:</b>")
            skill_names_ru = {
                "Pronunciation": "Произношение",
                "Grammar": "Грамматика",
                "Vocabulary": "Лексика",
                "Fluency": "Беглость"
            }
            for skill in ["Grammar", "Vocabulary", "Fluency"]:  # Skip pronunciation for now
                avg = averages[skill]
                if avg > 0:
                    bar = _def_score_bar(avg)
                    lines.append(f"  {skill_names_ru[skill]}: {bar} {avg}/5")

        return "\n".join(lines), markup
    else:
        period_names = {"all": "all time", "month": "this month", "week": "this week"}
        period_name = period_names.get(period, "selected period")

        lines = [
            f"<b>📊 Quick Stats ({period_name})</b>",
            "",
            f"Level: <b>{level}</b>",
            f"Tasks: <b>{total_sent}</b> | Replied: <b>{total_replied}</b> ({reply_rate}%)",
        ]

        if scored_skills:
            lines.append("")
            lines.append("<b>Average Scores:</b>")
            for skill in ["Grammar", "Vocabulary", "Fluency"]:  # Skip pronunciation for now
                avg = averages[skill]
                if avg > 0:
                    bar = _def_score_bar(avg)
                    lines.append(f"  {skill}: {bar} {avg}/5")

        return "\n".join(lines), markup


def _def_format_progress(chat_id: int) -> str:
    """Build the full /progress message for a student."""
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "ru")

    rows = get_student_task_log(chat_id)
    if not rows:
        if lang == "ru":
            return "История заданий не найдена. Выполните первое задание, чтобы начать отслеживать прогресс!"
        else:
            return "No task history found yet. Complete your first task to start tracking progress!"

    total_sent = len(rows)
    replied_rows = [r for r in rows if r.get("Reply Received")]
    total_replied = len(replied_rows)
    reply_rate = round(total_replied / total_sent * 100) if total_sent else 0
    streak = _week_streak(rows)

    level, _ = get_student_level_and_total_tasks(chat_id)

    # Use last 10 replied rows for skill averages
    last_10 = replied_rows[-10:]
    skills = ["Pronunciation", "Grammar", "Vocabulary", "Fluency"]
    averages = {}
    for skill in skills:
        vals = _def_scores(last_10, skill)
        averages[skill] = round(sum(vals) / len(vals), 1) if vals else 0.0

    scored_skills = {k: v for k, v in averages.items() if v > 0}
    weakest = min(scored_skills, key=scored_skills.get) if scored_skills else None

    if lang == "ru":
        streak_str = f"🔥 {streak} недел{'ь' if streak == 1 else ('и' if 2 <= streak <= 4 else 'ь')}" if streak >= 2 else ("✅ Активен на этой неделе" if streak == 1 else "Пока нет серии — ответьте на этой неделе, чтобы начать!")

        lines = [
            "<b>Ваш прогресс в SpeakUp</b>",
            "",
            f"Уровень: <b>{level}</b>  |  Заданий отправлено: <b>{total_sent}</b>",
            f"Ответов: <b>{total_replied}</b> из {total_sent} ({reply_rate}%)",
            f"Серия: {streak_str}",
        ]

        if scored_skills:
            lines += ["", "<b>Средние оценки навыков (последние 10 заданий):</b>"]
            skill_names_ru = {
                "Pronunciation": "Произношение",
                "Grammar": "Грамматика",
                "Vocabulary": "Лексика",
                "Fluency": "Беглость"
            }
            for skill in skills:
                avg = averages[skill]
                if avg > 0:
                    bar = _def_score_bar(avg)
                    label = _def_skill_label(avg)
                    ru_name = skill_names_ru.get(skill, skill)
                    lines.append(f"  {ru_name}: {bar} {avg}/5 — {label}")

        if weakest:
            weakest_ru = skill_names_ru.get(weakest, weakest)
            lines += ["", f"Область для фокуса: <b>{weakest_ru}</b> — продолжайте практиковаться, вы прогрессируете!"]

        return "\n".join(lines)
    else:
        # English version (existing code)
        streak_str = f"🔥 {streak} week{'s' if streak != 1 else ''} in a row" if streak >= 2 else ("✅ Active this week" if streak == 1 else "No streak yet — reply this week to start one!")

        lines = [
            "<b>Your SpeakUp Progress</b>",
            "",
            f"Level: <b>{level}</b>  |  Tasks sent: <b>{total_sent}</b>",
            f"Replied: <b>{total_replied}</b> of {total_sent} ({reply_rate}%)",
            f"Streak: {streak_str}",
        ]

        if scored_skills:
            lines += ["", "<b>Skill averages (last 10 tasks):</b>"]
            for skill in skills:
                avg = averages[skill]
                if avg > 0:
                    bar = _def_score_bar(avg)
                    label = _def_skill_label(avg)
                    lines.append(f"  {skill}: {bar} {avg}/5 — {label}")

        if weakest:
            lines += ["", f"Focus area: <b>{weakest}</b> — keep practising, you're making progress!"]

        return "\n".join(lines)


def _def_format_monthly_summary(chat_id: int, month_rows: List[dict], prev_rows: List[dict]) -> str:
    """Build a monthly progress summary for a student."""
    total = len(month_rows)
    replied = [r for r in month_rows if r.get("Reply Received")]
    rate = round(len(replied) / total * 100) if total else 0
    level, _ = get_student_level_and_total_tasks(chat_id)

    skills = ["Pronunciation", "Grammar", "Vocabulary", "Fluency"]
    curr_avgs = {}
    prev_avgs = {}
    for skill in skills:
        curr_vals = _def_scores(replied, skill)
        curr_avgs[skill] = round(sum(curr_vals) / len(curr_vals), 1) if curr_vals else 0.0
        prev_vals = _def_scores([r for r in prev_rows if r.get("Reply Received")], skill)
        prev_avgs[skill] = round(sum(prev_vals) / len(prev_vals), 1) if prev_vals else 0.0

    now = _now_moscow()
    prev_month_dt = (now.replace(day=1) - timedelta(days=1))
    month_name = prev_month_dt.strftime("%B")

    lines = [
        f"<b>Your {month_name} Progress Report</b>",
        "",
        f"Level: <b>{level}</b>",
        f"Tasks this month: <b>{total}</b> sent, <b>{len(replied)}</b> completed ({rate}%)",
        "",
        "<b>Skill scores:</b>",
    ]
    for skill in skills:
        avg = curr_avgs[skill]
        if avg > 0:
            trend = ""
            if prev_avgs[skill] > 0:
                diff = round(avg - prev_avgs[skill], 1)
                if diff > 0:
                    trend = f" (+{diff} vs last month)"
                elif diff < 0:
                    trend = f" ({diff} vs last month)"
            bar = _def_score_bar(avg)
            lines.append(f"  {skill}: {bar} {avg}/5{trend}")

    scored = {k: v for k, v in curr_avgs.items() if v > 0}
    if scored:
        best = max(scored, key=scored.get)
        worst = min(scored, key=scored.get)
        lines += [
            "",
            f"Biggest strength this month: <b>{best}</b>",
            f"Keep working on: <b>{worst}</b>",
        ]

    lines += ["", "Great work — see you next month! 💪"]
    return "\n".join(lines)


def _get_month_task_rows(chat_id: int, year: int, month: int) -> List[dict]:
    """Return Task Log rows for a given student and calendar month."""
    rows = get_student_task_log(chat_id)
    result = []
    for r in rows:
        try:
            dt = datetime.strptime(r.get("Date Sent", "")[:7], "%Y-%m")
            if dt.year == year and dt.month == month:
                result.append(r)
        except (ValueError, KeyError):
            continue
    return result


def _def_all_student_chat_ids() -> List[int]:
    """Return all chat IDs from the Students sheet (for bulk messaging)."""
    ws = get_students_worksheet()
    chat_ids = []
    for row in ws.get_all_records():
        try:
            chat_ids.append(int(row.get("Chat ID")))
        except (TypeError, ValueError):
            continue
    return chat_ids


def _generate_referral_code(chat_id: int) -> str:
    """Generate a unique referral code for a student."""
    import hashlib
    import time
    # Create deterministic but unique code based on chat_id and timestamp
    seed = f"{chat_id}_{int(time.time()) // 86400}"  # Changes daily for uniqueness
    code = hashlib.md5(seed.encode()).hexdigest()[:8].upper()
    return f"SPK{code}"


def _get_referral_stats(chat_id: int) -> dict:
    """Get referral statistics for a student."""
    settings = get_student_settings(chat_id)
    code = settings.get("Referral Code", "")
    if not code:
        # Generate code if doesn't exist
        code = _generate_referral_code(chat_id)
        update_student_setting(chat_id, "Referral Code", code)

    count = settings.get("Referral Count", 0)
    link = f"https://t.me/{BOT_TOKEN.split(':')[0]}?start=ref_{code}"

    return {
        "code": code,
        "count": count,
        "link": link,
        "reward_threshold": 3,  # Referrals needed for reward
        "reward_earned": count >= 3
    }


def _process_referral_join(new_chat_id: int, referral_code: str):
    """Process when a new student joins via referral link."""
    # Find the referrer
    ws = get_settings_worksheet()
    referrer_id = None
    for row in ws.get_all_records():
        if row.get("Referral Code") == referral_code:
            try:
                referrer_id = int(row.get("Chat ID"))
                break
            except (TypeError, ValueError):
                continue

    if referrer_id:
        # Increment referral count
        current_count = get_student_settings(referrer_id).get("Referral Count", 0)
        update_student_setting(referrer_id, "Referral Count", current_count + 1)

        # Store referral relationship (could add to separate sheet later)
        update_student_setting(new_chat_id, "Referred By", str(referrer_id))

        # Notify referrer if they hit reward threshold
        if current_count + 1 >= 3:
            try:
                bot.send_message(
                    referrer_id,
                    "🎉 Congratulations! You've successfully referred 3 friends.\n\n"
                    "You've earned a free month of premium access!\n"
                    "Contact your teacher to claim your reward."
                )
            except Exception as e:
                print(f"Could not notify referrer {referrer_id}: {e}")


def _get_analytics_data() -> dict:
    """Generate analytics data for dashboard."""
    students_ws = get_students_worksheet()
    task_ws = get_task_log_worksheet()
    settings_ws = get_settings_worksheet()

    all_students = students_ws.get_all_records()
    all_tasks = task_ws.get_all_records()
    all_settings = settings_ws.get_all_records()

    # Basic metrics
    total_students = len([s for s in all_students if s.get("Chat ID")])
    active_students = len([s for s in all_students if s.get("Level") and s.get("Chat ID")])

    # Level distribution
    levels = {}
    for student in all_students:
        level = student.get("Level", "")
        if level:
            levels[level] = levels.get(level, 0) + 1

    # Task completion stats
    total_tasks_sent = len(all_tasks)
    total_replies = len([t for t in all_tasks if t.get("Reply Received")])
    completion_rate = round(total_replies / total_tasks_sent * 100, 1) if total_tasks_sent else 0

    # Recent activity (last 30 days)
    now = _now_moscow()
    month_ago = now - timedelta(days=30)
    recent_tasks = []
    for task in all_tasks:
        try:
            task_date = datetime.strptime(task.get("Date Sent", "")[:10], "%Y-%m-%d")
            if task_date >= month_ago:
                recent_tasks.append(task)
        except (ValueError, KeyError):
            continue

    recent_replies = len([t for t in recent_tasks if t.get("Reply Received")])

    # Language preferences
    lang_stats = {}
    for setting in all_settings:
        lang = setting.get("Language", "en")
        lang_stats[lang] = lang_stats.get(lang, 0) + 1

    return {
        "total_students": total_students,
        "active_students": active_students,
        "level_distribution": levels,
        "total_tasks": total_tasks_sent,
        "total_replies": total_replies,
        "completion_rate": completion_rate,
        "recent_tasks": len(recent_tasks),
        "recent_replies": recent_replies,
        "language_stats": lang_stats
    }


def get_settings_worksheet():
    client = get_gspread_client()
    spreadsheet = client.open(SPREADSHEET_NAME)
    try:
        ws = spreadsheet.worksheet(SETTINGS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SETTINGS_SHEET_NAME, rows="1000", cols=len(SETTINGS_HEADERS))
        ws.append_row(SETTINGS_HEADERS)
    return ws


def get_student_settings(chat_id: int) -> dict:
    """Get student settings, with defaults."""
    ws = get_settings_worksheet()
    for row in ws.get_all_records():
        if str(row.get("Chat ID")) == str(chat_id):
            return row
    # Return defaults if no settings found
    return {
        "Chat ID": str(chat_id),
        "Language": "en",  # Default to English
        "Notifications": "on",
        "Referral Code": "",
        "Referral Count": 0,
        "Status": "active",
        "Last Activity": "",
        "Maintenance Mode": "off"
    }


def update_student_setting(chat_id: int, key: str, value: str):
    """Update a single setting for a student."""
    ws = get_settings_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        return

    headers = all_values[0]
    try:
        chat_id_col = headers.index("Chat ID")
        key_col = headers.index(key)
    except ValueError:
        return

    # Find existing row or create new one
    target_row_idx = None
    for i in range(1, len(all_values)):
        if str(all_values[i][chat_id_col]) == str(chat_id):
            target_row_idx = i + 1
            break

    if target_row_idx is None:
        # Create new row
        new_row = [str(chat_id) if col == chat_id_col else (value if headers[col] == key else "") for col in range(len(headers))]
        ws.append_row(new_row)
    else:
        # Update existing row
        ws.update_cell(target_row_idx, key_col + 1, value)


def is_maintenance_mode() -> bool:
    """Check if bot is in maintenance mode globally."""
    ws = get_settings_worksheet()
    try:
        # Look for admin row (chat_id = 0) maintenance setting
        for row in ws.get_all_records():
            if str(row.get("Chat ID")) == "0" and row.get("Maintenance Mode") == "on":
                return True
    except:
        pass
    return False


def set_maintenance_mode(enabled: bool):
    """Set global maintenance mode."""
    update_student_setting(0, "Maintenance Mode", "on" if enabled else "off")


def _def_get_all_levels() -> dict:
    """Return {chat_id: level} for all students."""
    ws = get_students_worksheet()
    result = {}
    for row in ws.get_all_records():
        try:
            cid = int(row.get("Chat ID"))
            result[cid] = str(row.get("Level", "")).strip()
        except (TypeError, ValueError):
            continue
    return result


def _get_task_list_worksheet(level: str):
    """Open the level's Task List sheet (e.g. A2 Task List)."""
    sheet_name = f"{level.strip().upper()} Task List"
    try:
        client = get_gspread_client()
        spreadsheet = client.open(SPREADSHEET_NAME)
        return spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        return None


def get_channel_message_id_for_task(level: str, task_number: int) -> Optional[int]:
    """Get the channel Message ID to forward for this Task # from the level's Task List (column 'Message ID')."""
    ws = _get_task_list_worksheet(level)
    if not ws:
        return None
    for row in ws.get_all_records():
        try:
            num = int(row.get("Task #", 0) or 0)
            if num == task_number:
                mid = row.get("Message ID") or row.get("Message id")
                return int(mid) if mid not in (None, "") else None
        except (TypeError, ValueError):
            continue
    return None


def get_task_script(level: str, task_number: int) -> str:
    """Get the Script Text for this Task # from the level's Task List sheet (e.g. A2 Task List)."""
    ws = _get_task_list_worksheet(level)
    if not ws:
        return ""
    for row in ws.get_all_records():
        try:
            num = int(row.get("Task #", 0) or 0)
            if num == task_number:
                return str(row.get("Script Text", row.get("Script text", "")) or "").strip()
        except (TypeError, ValueError):
            continue
    return ""


def get_task_example_answers(level: str, task_number: int) -> str:
    """Get the Example Answers for this Task # from the level's Task List sheet."""
    ws = _get_task_list_worksheet(level)
    if not ws:
        return ""
    for row in ws.get_all_records():
        try:
            num = int(row.get("Task #", 0) or 0)
            if num == task_number:
                return str(row.get("Example Answers", row.get("example answers", "")) or "").strip()
        except (TypeError, ValueError):
            continue
    return ""


# =======================
# REGISTRATION STATE (for step-by-step signup)
# =======================
def _load_registration_state() -> dict:
    if not os.path.exists(REGISTRATION_STATE_PATH):
        return {}
    try:
        import json
        with open(REGISTRATION_STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_registration_state(data: dict):
    import json
    with open(REGISTRATION_STATE_PATH, "w") as f:
        json.dump(data, f, indent=0)


def get_registration_state(chat_id: int) -> Optional[dict]:
    data = _load_registration_state()
    return data.get(str(chat_id))


def set_registration_state(chat_id: int, state: dict):
    data = _load_registration_state()
    data[str(chat_id)] = state
    _save_registration_state(data)


def clear_registration_state(chat_id: int):
    data = _load_registration_state()
    data.pop(str(chat_id), None)
    _save_registration_state(data)


# =======================
# TELEGRAM BOT SETUP
# =======================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Debug: Check bot connection
print("Bot starting...")
print(f"Bot token: {BOT_TOKEN[:15]}...")
try:
    bot_info = bot.get_me()
    print(f"✅ Bot connected: @{bot_info.username} (ID: {bot_info.id})")
except Exception as e:
    print(f"❌ Failed to connect to Telegram: {e}")
    print("Check your bot token!")
    print("Current token starts with:", BOT_TOKEN[:10] if BOT_TOKEN else "EMPTY")

print(f"Admin IDs: {ADMIN_IDS}")
print(f"Admin feedback chat: {ADMIN_FEEDBACK_CHAT_ID}")
print(f"Work chat: {WORK_CHAT_ID}")
print(f"Partner chat: {PARTNER_CHAT_ID}")
print(f"Level channels: {LEVEL_CHANNELS}")

# Maps message IDs in admin DM → student chat_id so reply detection
# works even when Telegram hides forward_from due to privacy settings.
_student_reply_map: dict = {}

# Stores pending practice exercise answers keyed by chat_id
_practice_state: dict = {}

# Stores pending /messageall targets keyed by admin user_id
_messageall_state: dict = {}

# In-memory activity log (last 50 entries)
_bot_logs: list = []

def _add_log(entry: str):
    now = _now_moscow().strftime("%Y-%m-%d %H:%M")
    _bot_logs.append(f"[{now}] {entry}")
    if len(_bot_logs) > 50:
        _bot_logs.pop(0)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# =======================
# REGISTRATION FLOW: /start → level (buttons) → name → phone (optional, Skip button)
# =======================
def _level_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    for level in LEVELS:
        markup.add(types.InlineKeyboardButton(level, callback_data=f"reg_level_{level}"))
    return markup


@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    chat_id = message.chat.id
    clear_registration_state(chat_id)
    # Handle referral code: /start ref_XXXXXXXX
    parts = message.text.strip().split()
    if len(parts) == 2 and parts[1].startswith("ref_"):
        referral_code = parts[1][4:]
        _process_referral_join(chat_id, referral_code)
    bot.reply_to(
        message,
        "👋 Welcome! Tap your <b>level</b> to get started:",
        reply_markup=_level_keyboard(),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("reg_level_"))
def handle_reg_level(callback: types.CallbackQuery):
    level = callback.data.replace("reg_level_", "")
    chat_id = callback.message.chat.id
    set_registration_state(chat_id, {"step": "name", "level": level})
    bot.answer_callback_query(callback.id)
    bot.send_message(chat_id, "✅ Level <b>" + level + "</b> selected.\n\nWhat is your <b>full name</b>? (Type it in the chat.)")


@bot.callback_query_handler(func=lambda c: c.data == "reg_skip_phone")
def handle_reg_skip_phone(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    state = get_registration_state(chat_id)
    if not state or state.get("step") != "phone":
        bot.answer_callback_query(callback.id)
        return
    u = callback.from_user
    handle = f"@{u.username}" if u and u.username else ""
    bot.answer_callback_query(callback.id)
    _finish_registration(chat_id, state, telephone="", telegram_handle=handle)
    clear_registration_state(chat_id)


def _finish_registration(chat_id: int, state: dict, telephone: str = "", telegram_handle: str = ""):
    name = state.get("name", "")
    level = state.get("level", "")
    register_student(chat_id, level, name=name, telegram_handle=telegram_handle, telephone=telephone)
    bot.send_message(
        chat_id,
        f"🎉 You're all set!\n\nLevel: <b>{level}</b>. You'll receive tasks here. Send /start to change your level later.",
    )


@bot.message_handler(func=lambda m: get_registration_state(m.chat.id) is not None)
def handle_registration_step(message: types.Message):
    chat_id = message.chat.id
    state = get_registration_state(chat_id)
    if not state:
        return
    step = state.get("step")
    if message.content_type != "text":
        bot.reply_to(message, "Please type your answer in the chat (no voice or photos).")
        return
    text = (message.text or "").strip()

    if step == "name":
        if not text:
            bot.reply_to(message, "Please type your full name.")
            return
        state["name"] = text
        state["step"] = "phone"
        set_registration_state(chat_id, state)
        skip_btn = types.InlineKeyboardMarkup().row(
            types.InlineKeyboardButton("Skip", callback_data="reg_skip_phone")
        )
        bot.reply_to(message, "Thanks! What's your <b>phone number</b>? (Optional — tap Skip if you prefer not to share.)", reply_markup=skip_btn)
        return

    if step == "phone":
        u = message.from_user
        handle = f"@{u.username}" if u and u.username else ""
        clear_registration_state(chat_id)
        _finish_registration(chat_id, state, telephone=text, telegram_handle=handle)
        return


# =======================
# COMMAND: /sendtask (admin only)
# =======================
@bot.message_handler(commands=["sendtask"])
def handle_sendtask(message: types.Message):
    """
    Usage (admin only): /sendtask <level>
    Sends each student in that level their *next* task (Total Tasks Sent + 1) and logs to Task Log.
    """
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "You are not authorized to use this command.")
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(
            message,
            "Usage: <code>/sendtask &lt;level&gt;</code>\n"
            "Example: <code>/sendtask A2</code> — sends each student their next task and logs to Task Log.",
        )
        return
    level = parts[1].strip().upper()
    channel_id = LEVEL_CHANNELS.get(level)
    if not channel_id:
        bot.reply_to(message, f"Unknown level <b>{level}</b>. Use one of: A1, A2, B1, B2.")
        return
    students = get_students_by_level(level)
    if not students:
        bot.reply_to(message, f"No students found for level <b>{level}</b>.")
        return
    sent_count = 0
    for student_chat_id in students:
        _, total = get_student_level_and_total_tasks(student_chat_id)
        next_task = total + 1
        message_id = get_channel_message_id_for_task(level, next_task)
        if message_id is None:
            print(f"Sendtask: no Task # {next_task} in {level} Task List for chat {student_chat_id}, skipping.")
            continue
        try:
            bot.forward_message(
                chat_id=student_chat_id,
                from_chat_id=channel_id,
                message_id=message_id,
            )
            script_text = get_task_script(level, next_task)
            if script_text:
                bot.send_message(student_chat_id, script_text)
            student_name = get_student_name(student_chat_id)
            append_task_log(student_name, next_task, level, chat_id=student_chat_id)
            sent_count += 1
        except Exception as exc:
            print(f"Failed to forward to {student_chat_id}: {exc}")
    bot.reply_to(
        message,
        f"Sent next task to {sent_count} student(s) in <b>{level}</b>. Task Log updated.",
    )

# =======================
# COMMAND: /sendtaskto (admin only, single user)
# =======================
@bot.message_handler(commands=["sendtaskto"])
def handle_sendtask_to(message: types.Message):
    """
    Usage (admin only): /sendtaskto <level> <chat_id>
    Sends that student their *next* task (Total Tasks Sent + 1) and logs to Task Log.
    """
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "You are not authorized to use this command.")
        return
    parts = message.text.strip().split()
    if len(parts) != 3:
        bot.reply_to(
            message,
            "Usage: <code>/sendtaskto &lt;level&gt; &lt;chat_id&gt;</code>\n"
            "Example: <code>/sendtaskto A2 123456789</code> — sends that chat their next task.",
        )
        return
    level = parts[1].strip().upper()
    channel_id = LEVEL_CHANNELS.get(level)
    if not channel_id:
        bot.reply_to(message, f"Unknown level <b>{level}</b>. Use: A1, A2, B1, B2.")
        return
    try:
        target_chat_id = int(parts[2])
    except ValueError:
        bot.reply_to(message, "chat_id must be an integer.")
        return
    _, total = get_student_level_and_total_tasks(target_chat_id)
    next_task = total + 1
    message_id = get_channel_message_id_for_task(level, next_task)
    if message_id is None:
        bot.reply_to(message, f"Next Task # <b>{next_task}</b> not in {level} Task List or no Message ID.")
        return
    try:
        bot.forward_message(
            chat_id=target_chat_id,
            from_chat_id=channel_id,
            message_id=message_id,
        )
        script_text = get_task_script(level, next_task)
        if script_text:
            bot.send_message(target_chat_id, script_text)
        student_name = get_student_name(target_chat_id)
        append_task_log(student_name, next_task, level, chat_id=target_chat_id)
    except Exception as exc:
        bot.reply_to(message, f"Failed to forward task: <code>{exc}</code>")
        return
    bot.reply_to(
        message,
        f"Sent {level} Task # <code>{next_task}</code> to chat <code>{target_chat_id}</code>. Task Log updated.",
    )

# =======================
# COMMAND: /help (interactive help menu)
# =======================
def _help_keyboard(is_admin: bool = False) -> types.InlineKeyboardMarkup:
    """Create help keyboard based on user role."""
    markup = types.InlineKeyboardMarkup(row_width=2)

    if is_admin:
        markup.add(
            types.InlineKeyboardButton("👥 Student Management", callback_data="help_admin_students"),
            types.InlineKeyboardButton("📊 Analytics & Reports", callback_data="help_admin_analytics"),
            types.InlineKeyboardButton("⚙️ System & Maintenance", callback_data="help_admin_system"),
            types.InlineKeyboardButton("📋 All Commands", callback_data="help_admin_commands"),
        )
    else:
        markup.add(
            types.InlineKeyboardButton("📚 Learning Commands", callback_data="help_student_learning"),
            types.InlineKeyboardButton("📊 Progress & Stats", callback_data="help_student_progress"),
            types.InlineKeyboardButton("⚙️ Settings & Account", callback_data="help_student_settings"),
            types.InlineKeyboardButton("📋 All Commands", callback_data="help_student_commands"),
        )

    markup.add(types.InlineKeyboardButton("❌ Close", callback_data="help_close"))
    return markup


@bot.message_handler(commands=["help"])
def handle_help(message: types.Message):
    chat_id = message.chat.id
    is_admin_user = is_admin(chat_id)

    settings = get_student_settings(chat_id) if not is_admin_user else {}
    lang = settings.get("Language", "en")

    if is_admin_user:
        intro = "🛠️ <b>Admin Help Menu</b>\n\nChoose a category below to see available commands and how to use them."
    else:
        intro = "📚 <b>SpeakUp Help Menu</b>\n\nChoose a category below to learn about available commands.\n\n💡 <i>Tip: You can also type commands directly or use the buttons below.</i>"

    if lang == "ru" and not is_admin_user:
        intro = "📚 <b>Меню помощи SpeakUp</b>\n\nВыберите категорию ниже, чтобы узнать о доступных командах.\n\n💡 <i>Подсказка: Вы можете вводить команды напрямую или использовать кнопки ниже.</i>"

    bot.send_message(chat_id, intro, reply_markup=_help_keyboard(is_admin_user))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("help_"))
def handle_help_callbacks(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data
    is_admin_user = is_admin(chat_id)

    settings = get_student_settings(chat_id) if not is_admin_user else {}
    lang = settings.get("Language", "en")

    bot.answer_callback_query(callback.id)

    if data == "help_close":
        bot.edit_message_text("Help menu closed. Type /help anytime to reopen!", chat_id, callback.message.message_id)
        return

    # Student help sections
    if data == "help_student_learning":
        text = """📚 <b>Learning Commands</b>

🎯 <b>/vocabulary</b> — Practice vocabulary with flashcards
✍️ <b>/practice</b> — Extra practice exercises (sentence building, etc.)
💡 <b>/tips</b> — Get personalized study tips based on your progress
📖 <b>/dictionary [word]</b> — Quick word lookup with examples
📝 <b>/examples [grammar]</b> — Example sentences for grammar points

<i>Example: /dictionary hello</i>"""
        if lang == "ru":
            text = """📚 <b>Команды обучения</b>

🎯 <b>/vocabulary</b> — Практика словарного запаса с flashcards
✍️ <b>/practice</b> — Дополнительные упражнения (построение предложений и т.д.)
💡 <b>/tips</b> — Персональные советы по изучению на основе вашего прогресса
📖 <b>/dictionary [слово]</b> — Быстрый поиск слова с примерами
📝 <b>/examples [грамматика]</b> — Примеры предложений для грамматических правил

<i>Пример: /dictionary hello</i>"""

    elif data == "help_student_progress":
        text = """📊 <b>Progress & Stats Commands</b>

📈 <b>/progress</b> — View your detailed progress report
📊 <b>/stats</b> — Quick statistics and skill averages
🔗 <b>/referral</b> — Get your referral link to invite friends
⏰ <b>/remind [hours]</b> — Set reminder for next task

<i>Your progress is automatically tracked and scored!</i>"""
        if lang == "ru":
            text = """📊 <b>Команды прогресса и статистики</b>

📈 <b>/progress</b> — Посмотреть детальный отчет о прогрессе
📊 <b>/stats</b> — Быстрая статистика и средние оценки навыков
🔗 <b>/referral</b> — Получить реферальную ссылку для приглашения друзей
⏰ <b>/remind [часы]</b> — Установить напоминание для следующего задания

<i>Ваш прогресс автоматически отслеживается и оценивается!</i>"""

    elif data == "help_student_settings":
        text = """⚙️ <b>Settings & Account Commands</b>

🌐 <b>Language Settings</b>
Use buttons below to switch languages:

<i>Current: English</i>"""
        if lang == "ru":
            text = """⚙️ <b>Настройки аккаунта</b>

🌐 <b>Настройки языка</b>
Используйте кнопки ниже для переключения языка:

<i>Текущий: Русский</i>"""

        # Add language toggle buttons
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🇺🇸 English", callback_data="lang_en"),
            types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")
        )
        markup.add(types.InlineKeyboardButton("⬅️ Back to Help", callback_data="help_back"))

        bot.edit_message_text(text, chat_id, callback.message.message_id, reply_markup=markup)
        return

    elif data == "help_student_commands":
        text = """📋 <b>All Student Commands</b>

/start — Register or change your level
/help — This help menu
/progress — Detailed progress report
/stats — Quick statistics
/vocabulary — Vocabulary flashcards
/practice — Extra practice exercises
/tips — Study tips
/dictionary [word] — Word lookup
/examples [grammar] — Grammar examples
/referral — Get referral link
/remind [hours] — Set task reminder
/settings — Account preferences"""
        if lang == "ru":
            text = """📋 <b>Все команды для студентов</b>

/start — Регистрация или смена уровня
/help — Это меню помощи
/progress — Детальный отчет о прогрессе
/stats — Быстрая статистика
/vocabulary — Флэшкарты словарного запаса
/practice — Дополнительные упражнения
/tips — Советы по изучению
/dictionary [слово] — Поиск слова
/examples [грамматика] — Примеры грамматики
/referral — Получить реферальную ссылку
/remind [часы] — Установить напоминание
/settings — Настройки аккаунта"""

    # Admin help sections
    elif data == "help_admin_students":
        text = """👥 <b>Student Management Commands</b>

📋 <b>/liststudents</b> — List all students by level
👤 <b>/studentinfo [chat_id]</b> — Detailed student profile
🚫 <b>/suspend [chat_id]</b> — Temporarily suspend student
✅ <b>/unsuspend [chat_id]</b> — Restore student access
📢 <b>/messageall [level]</b> — Send announcement to level
❌ <b>/kick [chat_id]</b> — Remove student from system

<i>Example: /studentinfo 123456789</i>"""

    elif data == "help_admin_analytics":
        text = """📊 <b>Analytics & Reports Commands</b>

📈 <b>/analytics</b> — Weekly/monthly statistics
📄 <b>/report [student]</b> — Detailed student report
👥 <b>/inactive [days]</b> — List inactive students

<i>Use /analytics to see completion rates and growth trends</i>"""

    elif data == "help_admin_system":
        text = """⚙️ <b>System & Maintenance Commands</b>

🔍 <b>/preview [level] [task#]</b> — Test task appearance
🧪 <b>/test</b> — Test admin chat connections
📝 <b>/logs</b> — View recent bot activity
🔧 <b>/maintenance [on/off]</b> — Enter/exit maintenance mode

<i>Use /test after bot restarts to verify connections</i>"""

    elif data == "help_admin_commands":
        text = """📋 <b>All Admin Commands</b>

/sendtask [level] — Send tasks to all students in level
/sendtaskto [level] [chat_id] — Send task to specific student
/liststudents — List students by level
/studentinfo [chat_id] — Student profile
/suspend [chat_id] — Suspend student
/unsuspend [chat_id] — Unsuspend student
/messageall [level] — Send announcement
/kick [chat_id] — Remove student
/progress [chat_id] — Check student progress
/analytics — View statistics
/report [student] — Generate report
/inactive [days] — Find inactive students
/preview [level] [task#] — Test task
/test — Test connections
/logs — View activity logs
/maintenance [on/off] — Maintenance mode"""

    elif data == "lang_en":
        update_student_setting(chat_id, "Language", "en")
        bot.answer_callback_query(callback.id, "Language set to English 🇺🇸")
        bot.edit_message_text("✅ Language changed to English!\n\nType /help to see the updated menu.", chat_id, callback.message.message_id)
        return

    elif data == "lang_ru":
        update_student_setting(chat_id, "Language", "ru")
        bot.answer_callback_query(callback.id, "Язык изменен на русский 🇷🇺")
        bot.edit_message_text("✅ Язык изменен на русский!\n\nВведите /help для обновленного меню.", chat_id, callback.message.message_id)
        return

    elif data == "help_back":
        intro = "📚 <b>SpeakUp Help Menu</b>\n\nChoose a category below to learn about available commands."
        if lang == "ru":
            intro = "📚 <b>Меню помощи SpeakUp</b>\n\nВыберите категорию ниже, чтобы узнать о доступных командах."
        bot.edit_message_text(intro, chat_id, callback.message.message_id, reply_markup=_help_keyboard(is_admin_user))
        return

    else:
        text = "Unknown help section."

    # Add back button to all help sections
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Back to Help", callback_data="help_back"))

    bot.edit_message_text(text, chat_id, callback.message.message_id, reply_markup=markup)


# =======================
# COMMAND: /progress (student-facing)
# =======================
@bot.message_handler(commands=["progress"])
def handle_progress(message: types.Message):
    chat_id = message.chat.id
    parts = message.text.strip().split()
    if len(parts) == 1:  # /progress — show own progress
        if is_maintenance_mode() and not is_admin(chat_id):
            bot.send_message(chat_id, "🔧 Bot is currently under maintenance. Please try again later.")
            return
        try:
            text = _def_format_progress(chat_id)
        except Exception as e:
            text = f"Could not load your progress right now. Please try again later. ({e})"
        bot.send_message(chat_id, text)
        return

    # Admin checking specific student: /progress <chat_id>
    if not is_admin(chat_id):
        bot.reply_to(message, "You are not authorized to check other students' progress.")
        return
    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/progress</code> (your own) or <code>/progress &lt;chat_id&gt;</code> (admin only)")
        return
    try:
        target_chat_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "chat_id must be a number.")
        return
    try:
        text = _def_format_progress(target_chat_id)
        # Add a note that this is admin view
        text = f"📊 Progress for student <code>{target_chat_id}</code>:\n\n{text}"
    except Exception as e:
        text = f"Could not load progress for that student. ({e})"
    bot.send_message(chat_id, text)


# =======================
# COMMAND: /stats
# =======================
@bot.message_handler(commands=["stats"])
def handle_stats(message: types.Message):
    chat_id = message.chat.id
    if is_maintenance_mode() and not is_admin(chat_id):
        bot.send_message(chat_id, "🔧 Bot is currently under maintenance. Please try again later.")
        return
    try:
        text, markup = _def_format_stats(chat_id, "all")
        bot.send_message(chat_id, text, reply_markup=markup)
    except Exception as e:
        bot.send_message(chat_id, f"Could not load stats right now. Please try again later. ({e})")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("stats_"))
def handle_stats_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    period = callback.data.replace("stats_", "")
    bot.answer_callback_query(callback.id)
    try:
        text, markup = _def_format_stats(chat_id, period)
        bot.edit_message_text(text, chat_id, callback.message.message_id, reply_markup=markup)
    except Exception as e:
        bot.answer_callback_query(callback.id, f"Error: {e}", show_alert=True)


# =======================
# COMMAND: /vocabulary
# =======================
@bot.message_handler(commands=["vocabulary"])
def handle_vocabulary(message: types.Message):
    chat_id = message.chat.id
    if is_maintenance_mode() and not is_admin(chat_id):
        bot.send_message(chat_id, "🔧 Bot is currently under maintenance.")
        return
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "en")
    level, _ = get_student_level_and_total_tasks(chat_id)

    rows = get_student_task_log(chat_id)
    replied = [r for r in rows if r.get("Reply Received")][-10:]
    scores = {}
    for skill in ["Grammar", "Vocabulary", "Fluency"]:
        vals = _def_scores(replied, skill)
        scores[skill] = round(sum(vals) / len(vals), 1) if vals else 0.0
    weak = [k for k, v in scores.items() if v > 0 and v < 3.5]

    wait_msg = bot.send_message(chat_id, "🎯 Generating flashcards..." if lang == "en" else "🎯 Генерирую карточки словарного запаса...")
    cards = _generate_vocabulary_flashcards(level, weak)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass

    if not cards:
        bot.send_message(chat_id, "Could not generate flashcards right now. Please try again later.")
        return

    if lang == "ru":
        text = f"🎯 <b>Карточки словарного запаса — уровень {level}</b>\n\n"
        for i, c in enumerate(cards, 1):
            synonyms = c.get("synonyms", [])
            syn_str = f"\n🔄 <i>Синонимы: {', '.join(synonyms)}</i>" if synonyms else ""
            text += (
                f"<b>{i}. {c.get('word', '')}</b>\n"
                f"📖 {c.get('definition', '')}\n"
                f"💬 <i>{c.get('example', '')}</i>{syn_str}\n\n"
            )
    else:
        text = f"🎯 <b>Vocabulary Flashcards — {level} Level</b>\n\n"
        for i, c in enumerate(cards, 1):
            synonyms = c.get("synonyms", [])
            syn_str = f"\n🔄 <i>Synonyms: {', '.join(synonyms)}</i>" if synonyms else ""
            text += (
                f"<b>{i}. {c.get('word', '')}</b> ({c.get('part_of_speech', c.get('category', ''))})\n"
                f"📖 {c.get('definition', '')}\n"
                f"💬 <i>{c.get('example', '')}</i>{syn_str}\n\n"
            )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "🔄 New Cards" if lang == "en" else "🔄 Новые карточки",
        callback_data="vocab_refresh"
    ))
    bot.send_message(chat_id, text, reply_markup=markup)
    _add_log(f"vocab: chat_id={chat_id} level={level}")


@bot.callback_query_handler(func=lambda c: c.data == "vocab_refresh")
def handle_vocab_refresh(callback: types.CallbackQuery):
    bot.answer_callback_query(callback.id)
    handle_vocabulary(callback.message)


# =======================
# COMMAND: /tips
# =======================
@bot.message_handler(commands=["tips"])
def handle_tips(message: types.Message):
    chat_id = message.chat.id
    if is_maintenance_mode() and not is_admin(chat_id):
        bot.send_message(chat_id, "🔧 Bot is currently under maintenance.")
        return
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "en")
    level, _ = get_student_level_and_total_tasks(chat_id)

    wait_msg = bot.send_message(chat_id, "💡 Analyzing your progress..." if lang == "en" else "💡 Анализирую ваш прогресс...")

    rows = get_student_task_log(chat_id)
    replied = [r for r in rows if r.get("Reply Received")][-10:]
    transcripts = [r.get("Transcript", "") for r in replied if r.get("Transcript")]
    scores = {}
    for skill in ["Pronunciation", "Grammar", "Vocabulary", "Fluency"]:
        vals = _def_scores(replied, skill)
        scores[skill] = round(sum(vals) / len(vals), 1) if vals else 0.0

    tips = _generate_personalized_tips(transcripts, scores, level)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass

    if lang == "ru":
        header = f"💡 <b>Персональные советы по изучению (уровень {level})</b>\n\n"
    else:
        header = f"💡 <b>Personalized Study Tips ({level} level)</b>\n\n"
    bot.send_message(chat_id, header + tips)
    _add_log(f"tips: chat_id={chat_id} level={level}")


# =======================
# COMMAND: /practice
# =======================
@bot.message_handler(commands=["practice"])
def handle_practice(message: types.Message):
    chat_id = message.chat.id
    if is_maintenance_mode() and not is_admin(chat_id):
        bot.send_message(chat_id, "🔧 Bot is currently under maintenance.")
        return
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "en")
    level, _ = get_student_level_and_total_tasks(chat_id)

    rows = get_student_task_log(chat_id)
    replied = [r for r in rows if r.get("Reply Received")][-10:]
    scores = {}
    for skill in ["Grammar", "Vocabulary", "Fluency"]:
        vals = _def_scores(replied, skill)
        scores[skill] = round(sum(vals) / len(vals), 1) if vals else 0.0
    weak = [k for k, v in scores.items() if v > 0 and v < 3.5]

    wait_msg = bot.send_message(chat_id, "✍️ Generating exercise..." if lang == "en" else "✍️ Генерирую упражнение...")
    exercise = _generate_practice_exercise(level, weak)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass

    if exercise.get("type") == "error":
        bot.send_message(chat_id, exercise.get("content", "Could not generate exercise."))
        return

    if lang == "ru":
        text = (
            f"✍️ <b>Упражнение ({exercise.get('skill_focus', 'Grammar')})</b>\n\n"
            f"{exercise.get('exercise', '')}\n\n"
            f"<i>Сложность: {exercise.get('difficulty', 'medium')}</i>"
        )
        show_btn = "👁 Показать ответ"
        new_btn = "🔄 Новое упражнение"
    else:
        text = (
            f"✍️ <b>Practice Exercise ({exercise.get('skill_focus', 'Grammar')})</b>\n\n"
            f"{exercise.get('exercise', '')}\n\n"
            f"<i>Difficulty: {exercise.get('difficulty', 'medium')}</i>"
        )
        show_btn = "👁 Show Answer"
        new_btn = "🔄 New Exercise"

    _practice_state[chat_id] = {
        "answer": exercise.get("correct_answer", ""),
        "explanation": exercise.get("explanation", ""),
        "lang": lang,
    }

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(show_btn, callback_data="practice_answer"),
        types.InlineKeyboardButton(new_btn, callback_data="practice_new"),
    )
    bot.send_message(chat_id, text, reply_markup=markup)
    _add_log(f"practice: chat_id={chat_id} level={level} weak={weak}")


@bot.callback_query_handler(func=lambda c: c.data in ("practice_answer", "practice_new"))
def handle_practice_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    bot.answer_callback_query(callback.id)

    if callback.data == "practice_answer":
        state = _practice_state.get(chat_id)
        if not state:
            bot.answer_callback_query(callback.id, "Session expired. Use /practice for a new exercise.", show_alert=True)
            return
        lang = state.get("lang", "en")
        if lang == "ru":
            text = f"✅ <b>Ответ:</b> {state['answer']}\n\n💡 <b>Объяснение:</b> {state['explanation']}"
        else:
            text = f"✅ <b>Answer:</b> {state['answer']}\n\n💡 <b>Explanation:</b> {state['explanation']}"
        new_btn = "🔄 Новое упражнение" if lang == "ru" else "🔄 New Exercise"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(new_btn, callback_data="practice_new"))
        bot.edit_message_text(
            callback.message.text + f"\n\n{text}",
            chat_id, callback.message.message_id, reply_markup=markup
        )
    else:
        # Trigger a new exercise as if sending /practice
        class _FakeMsg:
            def __init__(self, cid):
                self.chat = type("C", (), {"id": cid})()
                self.text = "/practice"
                self.from_user = type("U", (), {"id": cid})()
                self.content_type = "text"
        handle_practice(_FakeMsg(chat_id))


# =======================
# COMMAND: /dictionary
# =======================
@bot.message_handler(commands=["dictionary"])
def handle_dictionary(message: types.Message):
    chat_id = message.chat.id
    if is_maintenance_mode() and not is_admin(chat_id):
        bot.send_message(chat_id, "🔧 Bot is currently under maintenance.")
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: <code>/dictionary [word]</code>\nExample: <code>/dictionary perseverance</code>")
        return

    word = parts[1].strip()
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "en")
    level, _ = get_student_level_and_total_tasks(chat_id)

    wait_msg = bot.send_message(chat_id, f"📖 Looking up '{word}'...")
    result = _lookup_word(word, level)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass

    examples = result.get("examples", [])
    synonyms = result.get("synonyms", [])
    if isinstance(synonyms, list):
        synonyms = [str(s) for s in synonyms]

    if lang == "ru":
        text = f"📖 <b>{word}</b> ({result.get('part_of_speech', '')})\n\n<b>Определение:</b> {result.get('definition', '')}\n"
        if examples:
            text += "\n<b>Примеры:</b>\n" + "\n".join(f"• <i>{ex}</i>" for ex in examples)
        if synonyms:
            text += f"\n\n<b>Синонимы:</b> {', '.join(synonyms)}"
    else:
        text = f"📖 <b>{word}</b> ({result.get('part_of_speech', '')})\n\n<b>Definition:</b> {result.get('definition', '')}\n"
        if examples:
            text += "\n<b>Examples:</b>\n" + "\n".join(f"• <i>{ex}</i>" for ex in examples)
        if synonyms:
            text += f"\n\n<b>Synonyms:</b> {', '.join(synonyms)}"

    bot.send_message(chat_id, text)
    _add_log(f"dictionary: chat_id={chat_id} word={word}")


# =======================
# COMMAND: /examples
# =======================
@bot.message_handler(commands=["examples"])
def handle_examples(message: types.Message):
    chat_id = message.chat.id
    if is_maintenance_mode() and not is_admin(chat_id):
        bot.send_message(chat_id, "🔧 Bot is currently under maintenance.")
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: <code>/examples [grammar point]</code>\nExample: <code>/examples present continuous</code>")
        return

    grammar_point = parts[1].strip()
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "en")
    level, _ = get_student_level_and_total_tasks(chat_id)

    wait_msg = bot.send_message(chat_id, f"📝 Generating examples for '{grammar_point}'...")
    raw = _generate_grammar_examples(grammar_point, level)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass

    # Normalize to list of strings
    if isinstance(raw, dict):
        examples = raw.get("examples", raw.get("sentences", [str(raw)]))
    elif isinstance(raw, list):
        examples = [e.get("sentence", str(e)) if isinstance(e, dict) else str(e) for e in raw]
    else:
        examples = [str(raw)]

    if lang == "ru":
        header = f"📝 <b>Примеры: {grammar_point}</b>\n\n"
    else:
        header = f"📝 <b>Examples: {grammar_point}</b>\n\n"

    text = header + "\n".join(f"{i+1}. <i>{ex}</i>" for i, ex in enumerate(examples))
    bot.send_message(chat_id, text)
    _add_log(f"examples: chat_id={chat_id} grammar={grammar_point}")


# =======================
# COMMAND: /referral
# =======================
@bot.message_handler(commands=["referral"])
def handle_referral(message: types.Message):
    chat_id = message.chat.id
    if is_maintenance_mode() and not is_admin(chat_id):
        bot.send_message(chat_id, "🔧 Bot is currently under maintenance.")
        return
    settings = get_student_settings(chat_id)
    lang = settings.get("Language", "en")
    stats = _get_referral_stats(chat_id)

    code = stats["code"]
    count = int(stats["count"] or 0)
    threshold = stats["reward_threshold"]
    remaining = max(0, threshold - count)

    try:
        bot_username = bot.get_me().username
        link = f"https://t.me/{bot_username}?start=ref_{code}"
    except Exception:
        link = f"Code: <code>{code}</code>"

    if lang == "ru":
        text = (
            f"🔗 <b>Ваша реферальная ссылка</b>\n\n"
            f"Пригласите друзей и получите вознаграждение!\n\n"
            f"<b>Ссылка:</b> {link}\n"
            f"<b>Код:</b> <code>{code}</code>\n"
            f"<b>Приглашено друзей:</b> {count}\n\n"
        )
        if count >= threshold:
            text += "🎉 Вы заработали бесплатный месяц! Свяжитесь с учителем для активации."
        else:
            text += f"Пригласите ещё {remaining} {'друга' if remaining == 1 else 'друзей'} и получите <b>бесплатный месяц!</b>"
    else:
        text = (
            f"🔗 <b>Your Referral Link</b>\n\n"
            f"Invite friends and earn rewards!\n\n"
            f"<b>Link:</b> {link}\n"
            f"<b>Code:</b> <code>{code}</code>\n"
            f"<b>Friends referred:</b> {count}\n\n"
        )
        if count >= threshold:
            text += "🎉 You've earned a free month! Contact your teacher to claim it."
        else:
            text += f"Refer {remaining} more friend{'s' if remaining != 1 else ''} to earn a <b>free month!</b>"

    bot.send_message(chat_id, text)


# =======================
# COMMANDS: Admin Student Management
# =======================
@bot.message_handler(commands=["liststudents"])
def handle_liststudents(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    ws = get_students_worksheet()
    all_students = ws.get_all_records()
    if not all_students:
        bot.reply_to(message, "No students found.")
        return

    by_level: dict = {}
    for student in all_students:
        level = str(student.get("Level", "") or "Unknown").strip() or "Unknown"
        by_level.setdefault(level, []).append(student)

    text = "👥 <b>All Students</b>\n\n"
    for level in sorted(by_level.keys()):
        students_in_level = by_level[level]
        text += f"<b>{level}</b> ({len(students_in_level)}):\n"
        for s in students_in_level:
            name = str(s.get("Name", "") or "Unknown").strip() or "Unknown"
            cid = s.get("Chat ID", "")
            text += f"  • {name} — <code>{cid}</code>\n"
        text += "\n"

    bot.reply_to(message, text)
    _add_log(f"liststudents: admin={message.from_user.id}")


@bot.message_handler(commands=["studentinfo"])
def handle_studentinfo(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/studentinfo &lt;chat_id&gt;</code>")
        return
    try:
        target_chat_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "chat_id must be a number.")
        return

    row = get_student_row(target_chat_id)
    if not row:
        bot.reply_to(message, f"Student <code>{target_chat_id}</code> not found.")
        return

    settings = get_student_settings(target_chat_id)
    task_rows = get_student_task_log(target_chat_id)
    replied = [r for r in task_rows if r.get("Reply Received")]
    reply_rate = round(len(replied) / len(task_rows) * 100) if task_rows else 0

    text = (
        f"👤 <b>Student Profile</b>\n\n"
        f"<b>Name:</b> {row.get('Name', 'N/A')}\n"
        f"<b>Chat ID:</b> <code>{target_chat_id}</code>\n"
        f"<b>Level:</b> {row.get('Level', 'N/A')}\n"
        f"<b>Handle:</b> {row.get('Telegram Handle', 'N/A')}\n"
        f"<b>Phone:</b> {row.get('Telephone', 'N/A')}\n"
        f"<b>Tier End Date:</b> {row.get('Tier End Date', 'N/A')}\n"
        f"<b>Balance Due:</b> {row.get('Balance Due', 'N/A')}\n\n"
        f"<b>Tasks Sent:</b> {len(task_rows)}\n"
        f"<b>Tasks Replied:</b> {len(replied)} ({reply_rate}%)\n"
        f"<b>Language:</b> {settings.get('Language', 'en')}\n"
        f"<b>Status:</b> {settings.get('Status', 'active')}\n"
        f"<b>Notes:</b> {row.get('Notes', 'None') or 'None'}"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Full Progress", callback_data=f"adminprog_{target_chat_id}"),
        types.InlineKeyboardButton("🚫 Suspend", callback_data=f"adminsuspend_{target_chat_id}"),
    )
    bot.reply_to(message, text, reply_markup=markup)
    _add_log(f"studentinfo: admin={message.from_user.id} target={target_chat_id}")


@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith("adminprog_") or c.data.startswith("adminsuspend_")))
def handle_admin_student_actions(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        bot.answer_callback_query(callback.id, "Not authorized.", show_alert=True)
        return
    bot.answer_callback_query(callback.id)
    data = callback.data

    if data.startswith("adminprog_"):
        target_chat_id = int(data.replace("adminprog_", ""))
        try:
            text = _def_format_progress(target_chat_id)
            bot.send_message(callback.message.chat.id, f"📊 Progress for <code>{target_chat_id}</code>:\n\n{text}")
        except Exception as e:
            bot.send_message(callback.message.chat.id, f"Could not load progress: {e}")

    elif data.startswith("adminsuspend_"):
        target_chat_id = int(data.replace("adminsuspend_", ""))
        update_student_setting(target_chat_id, "Status", "suspended")
        name = get_student_name(target_chat_id)
        bot.send_message(callback.message.chat.id, f"🚫 <b>{name}</b> (<code>{target_chat_id}</code>) suspended.")
        try:
            bot.send_message(target_chat_id, "⚠️ Your account has been temporarily suspended. Please contact your teacher.")
        except Exception:
            pass
        _add_log(f"suspend: admin={callback.from_user.id} target={target_chat_id}")


@bot.message_handler(commands=["suspend"])
def handle_suspend(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/suspend &lt;chat_id&gt;</code>")
        return
    try:
        target_chat_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "chat_id must be a number.")
        return
    update_student_setting(target_chat_id, "Status", "suspended")
    name = get_student_name(target_chat_id)
    bot.reply_to(message, f"🚫 <b>{name}</b> (<code>{target_chat_id}</code>) has been suspended.")
    try:
        bot.send_message(target_chat_id, "⚠️ Your account has been temporarily suspended. Please contact your teacher.")
    except Exception as e:
        bot.reply_to(message, f"Note: Could not notify student: {e}")
    _add_log(f"suspend: admin={message.from_user.id} target={target_chat_id}")


@bot.message_handler(commands=["unsuspend"])
def handle_unsuspend(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/unsuspend &lt;chat_id&gt;</code>")
        return
    try:
        target_chat_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "chat_id must be a number.")
        return
    update_student_setting(target_chat_id, "Status", "active")
    name = get_student_name(target_chat_id)
    bot.reply_to(message, f"✅ <b>{name}</b> (<code>{target_chat_id}</code>) has been unsuspended.")
    try:
        bot.send_message(target_chat_id, "✅ Your account has been reactivated! Welcome back.")
    except Exception as e:
        bot.reply_to(message, f"Note: Could not notify student: {e}")
    _add_log(f"unsuspend: admin={message.from_user.id} target={target_chat_id}")


@bot.message_handler(commands=["messageall"])
def handle_messageall(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Usage: <code>/messageall &lt;level|all&gt; &lt;message&gt;</code>\nExample: <code>/messageall A2 Class is cancelled today.</code>")
        return

    level_target = parts[1].strip().upper()
    broadcast_text = parts[2].strip()

    if level_target != "ALL" and level_target not in LEVEL_CHANNELS:
        bot.reply_to(message, f"Invalid level. Use: {', '.join(LEVEL_CHANNELS.keys())} or ALL")
        return

    if level_target == "ALL":
        recipients = _def_all_student_chat_ids()
    else:
        recipients = get_students_by_level(level_target)

    sent = 0
    failed = 0
    for cid in recipients:
        try:
            bot.send_message(cid, f"📢 <b>Message from SpeakUp:</b>\n\n{broadcast_text}")
            sent += 1
        except Exception:
            failed += 1

    bot.reply_to(message, f"📢 Broadcast complete.\n✅ Sent: {sent} | ❌ Failed: {failed}")
    _add_log(f"messageall: admin={message.from_user.id} level={level_target} sent={sent}")


@bot.message_handler(commands=["kick"])
def handle_kick(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/kick &lt;chat_id&gt;</code>")
        return
    try:
        target_chat_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "chat_id must be a number.")
        return

    name = get_student_name(target_chat_id)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Confirm", callback_data=f"kickconfirm_{target_chat_id}"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="kickcancel"),
    )
    bot.reply_to(
        message,
        f"⚠️ Remove <b>{name}</b> (<code>{target_chat_id}</code>) from the system?\n\nThis deletes their Students sheet entry.",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith("kickconfirm_") or c.data == "kickcancel"))
def handle_kick_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        bot.answer_callback_query(callback.id, "Not authorized.", show_alert=True)
        return
    bot.answer_callback_query(callback.id)
    if callback.data == "kickcancel":
        bot.edit_message_text("Kick cancelled.", callback.message.chat.id, callback.message.message_id)
        return

    target_chat_id = int(callback.data.replace("kickconfirm_", ""))
    name = get_student_name(target_chat_id)
    try:
        ws = get_students_worksheet()
        all_values = ws.get_all_values()
        headers = all_values[0]
        chat_id_col = headers.index("Chat ID")
        for i, row in enumerate(all_values[1:], start=2):
            if chat_id_col < len(row) and str(row[chat_id_col]).strip() == str(target_chat_id):
                ws.delete_rows(i)
                break
        bot.edit_message_text(f"✅ <b>{name}</b> (<code>{target_chat_id}</code>) removed from the system.", callback.message.chat.id, callback.message.message_id)
        _add_log(f"kick: admin={callback.from_user.id} target={target_chat_id}")
    except Exception as e:
        bot.edit_message_text(f"❌ Could not remove student: {e}", callback.message.chat.id, callback.message.message_id)


# =======================
# COMMANDS: Admin Analytics
# =======================
@bot.message_handler(commands=["analytics"])
def handle_analytics(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    wait_msg = bot.reply_to(message, "📊 Loading analytics...")
    data = _get_analytics_data()
    try:
        bot.delete_message(message.chat.id, wait_msg.message_id)
    except Exception:
        pass

    level_dist = "\n".join([f"  {k}: {v}" for k, v in sorted(data["level_distribution"].items())]) or "  No data"
    now = _now_moscow()

    text = (
        f"📊 <b>SpeakUp Analytics</b>\n"
        f"<i>{now.strftime('%Y-%m-%d %H:%M')} Moscow</i>\n\n"
        f"<b>👥 Students:</b>\n"
        f"  Total: {data['total_students']} | Active: {data['active_students']}\n\n"
        f"<b>📚 Level Distribution:</b>\n{level_dist}\n\n"
        f"<b>📋 All Time:</b>\n"
        f"  Tasks Sent: {data['total_tasks']} | Replied: {data['total_replies']}\n"
        f"  Completion Rate: {data['completion_rate']}%\n\n"
        f"<b>📅 Last 30 Days:</b>\n"
        f"  Tasks Sent: {data['recent_tasks']} | Replied: {data['recent_replies']}\n\n"
        f"<b>🌐 Languages:</b>\n"
        f"  English: {data['language_stats'].get('en', 0)} | Russian: {data['language_stats'].get('ru', 0)}"
    )
    bot.reply_to(message, text)
    _add_log(f"analytics: admin={message.from_user.id}")


@bot.message_handler(commands=["report"])
def handle_report(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/report &lt;chat_id&gt;</code>")
        return
    try:
        target_chat_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "chat_id must be a number.")
        return

    name = get_student_name(target_chat_id)
    try:
        progress = _def_format_progress(target_chat_id)
        row = get_student_row(target_chat_id)
        header = (
            f"📄 <b>Full Report — {name}</b>\n"
            f"Chat ID: <code>{target_chat_id}</code>\n"
            f"Level: {row.get('Level', 'N/A') if row else 'N/A'}\n"
            f"Tier End: {row.get('Tier End Date', 'N/A') if row else 'N/A'}\n"
            f"Balance Due: {row.get('Balance Due', 'N/A') if row else 'N/A'}\n\n"
        )
        bot.reply_to(message, header + progress)
    except Exception as e:
        bot.reply_to(message, f"Could not generate report: {e}")
    _add_log(f"report: admin={message.from_user.id} target={target_chat_id}")


@bot.message_handler(commands=["inactive"])
def handle_inactive(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    days = 7
    if len(parts) == 2:
        try:
            days = int(parts[1])
        except ValueError:
            bot.reply_to(message, "Usage: <code>/inactive [days]</code> (default: 7)")
            return

    now = _now_moscow()
    cutoff = now - timedelta(days=days)

    task_ws = get_task_log_worksheet()
    all_tasks = task_ws.get_all_records()

    last_reply: dict = {}
    for task in all_tasks:
        reply_date = task.get("Reply Received", "")
        cid = str(task.get("Chat ID", "")).strip()
        if not cid or not reply_date:
            continue
        try:
            dt = datetime.strptime(reply_date[:16], "%Y-%m-%d %H:%M")
            if cid not in last_reply or dt > last_reply[cid]:
                last_reply[cid] = dt
        except (ValueError, KeyError):
            continue

    ws = get_students_worksheet()
    inactive = []
    for row in ws.get_all_records():
        cid_str = str(row.get("Chat ID", "") or "").strip()
        if not cid_str:
            continue
        last_dt = last_reply.get(cid_str)
        if last_dt is None or last_dt < cutoff:
            days_ago = (now - last_dt).days if last_dt else 999
            inactive.append((str(row.get("Name", "Unknown") or "Unknown"), cid_str, days_ago))

    if not inactive:
        bot.reply_to(message, f"✅ All students replied within the last {days} days!")
        return

    text = f"⚠️ <b>Inactive Students ({days}+ days)</b>\n\n"
    for name, cid, d in sorted(inactive, key=lambda x: x[2], reverse=True):
        d_str = f"{d}d" if d < 999 else "never replied"
        text += f"• <b>{name}</b> (<code>{cid}</code>) — {d_str}\n"
    bot.reply_to(message, text)
    _add_log(f"inactive: admin={message.from_user.id} days={days} found={len(inactive)}")


# =======================
# COMMANDS: Admin Utilities
# =======================
@bot.message_handler(commands=["preview"])
def handle_preview(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 3:
        bot.reply_to(message, "Usage: <code>/preview &lt;level&gt; &lt;task_number&gt;</code>\nExample: <code>/preview A2 5</code>")
        return
    level = parts[1].strip().upper()
    channel_id = LEVEL_CHANNELS.get(level)
    if not channel_id:
        bot.reply_to(message, f"Unknown level. Use: {', '.join(LEVEL_CHANNELS.keys())}")
        return
    try:
        task_num = int(parts[2])
    except ValueError:
        bot.reply_to(message, "Task number must be an integer.")
        return

    message_id = get_channel_message_id_for_task(level, task_num)
    if message_id is None:
        bot.reply_to(message, f"Task #{task_num} not found in {level} Task List.")
        return

    script = get_task_script(level, task_num)
    bot.reply_to(message, f"👁 <b>Preview: {level} Task #{task_num}</b>")
    try:
        bot.forward_message(message.chat.id, channel_id, message_id)
        if script:
            bot.send_message(message.chat.id, f"📝 <b>Script text:</b>\n\n{script}")
        else:
            bot.send_message(message.chat.id, "⚠️ No script text found for this task.")
    except Exception as e:
        bot.reply_to(message, f"Could not forward: {e}")
    _add_log(f"preview: admin={message.from_user.id} level={level} task={task_num}")


@bot.message_handler(commands=["test"])
def handle_test(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    results = []
    test_msg = "🧪 SpeakUp bot connection test"

    for label, cid in [("Admin DM", ADMIN_FEEDBACK_CHAT_ID), ("Work Chat", WORK_CHAT_ID), ("Partner DM", PARTNER_CHAT_ID)]:
        try:
            bot.send_message(cid, test_msg)
            results.append(f"✅ {label} (<code>{cid}</code>)")
        except Exception as e:
            results.append(f"❌ {label}: {e}")

    bot.reply_to(message, "🧪 <b>Connection Test:</b>\n\n" + "\n".join(results))
    _add_log(f"test: admin={message.from_user.id}")


@bot.message_handler(commands=["logs"])
def handle_logs(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    if not _bot_logs:
        bot.reply_to(message, "📝 No activity logged yet.")
        return
    recent = _bot_logs[-20:]
    text = "📝 <b>Recent Activity (last 20)</b>\n\n" + "\n".join(recent)
    bot.reply_to(message, text)


@bot.message_handler(commands=["maintenance"])
def handle_maintenance(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        bot.reply_to(message, "Usage: <code>/maintenance on</code> or <code>/maintenance off</code>")
        return
    enabled = parts[1].lower() == "on"
    set_maintenance_mode(enabled)
    status = "ON 🔧" if enabled else "OFF ✅"
    bot.reply_to(message, f"Maintenance mode: <b>{status}</b>")
    _add_log(f"maintenance: admin={message.from_user.id} mode={'on' if enabled else 'off'}")


# =======================
# VOICE HANDLER (with AI transcription + draft)
# =======================
def _transcribe_audio(temp_path: str) -> str:
    """Transcribe audio file using Whisper. Returns transcript string."""
    transcript = "[Transcription unavailable - set OPENAI_API_KEY]"
    if openai_client:
        try:
            with open(temp_path, "rb") as audio_file:
                transcript_resp = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="verbose_json",
                    prompt="Transcribe the entire recording. Include all speech from start to end, even after long pauses.",
                )
            # Join all segments so we don't miss anything after pauses
            segments = getattr(transcript_resp, "segments", None)
            if segments:
                parts = []
                for s in segments:
                    t = s.get("text", "") if isinstance(s, dict) else getattr(s, "text", "")
                    if t:
                        parts.append(t.strip())
                transcript = " ".join(parts).strip() or "(empty)"
            else:
                transcript = (getattr(transcript_resp, "text", None) or "").strip() or "(empty)"
        except Exception as e:
            transcript = f"[Whisper error: {e}]"
    return transcript


def _run_transcribe_and_draft(temp_path: str, level: str) -> tuple:
    """Returns (transcript, ai_draft). DEPRECATED: use _transcribe_audio + _run_draft_feedback_and_score instead."""
    transcript = _transcribe_audio(temp_path)
    # For practice mode, use the advanced feedback with task context if available
    ai_draft, _ = _run_draft_feedback_and_score(transcript, level, "", "")
    return transcript, ai_draft


def _generate_vocabulary_flashcards(level: str, weak_areas: List[str] = None) -> List[dict]:
    """Generate 5-7 vocabulary flashcards based on student level and weak areas."""
    if not openai_client:
        return []

    weak_focus = ""
    if weak_areas and "vocabulary" in [a.lower() for a in weak_areas]:
        weak_focus = " Focus especially on vocabulary gaps that students at this level commonly have."

    prompt = f"""Generate 5 vocabulary flashcards for {level} level English students.

Each flashcard should be a JSON object with:
- "word": the vocabulary word/phrase
- "definition": clear, simple definition
- "example": a natural example sentence using the word
- "difficulty": "beginner", "intermediate", or "advanced"
- "category": "academic", "everyday", "business", or "general"

{weak_focus}

Respond with a JSON array of 5 flashcards."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.8,
            response_format={"type": "json_object"},
        )
        import json as _json
        data = _json.loads(response.choices[0].message.content.strip())
        return data.get("flashcards", []) if isinstance(data, dict) else data
    except Exception as e:
        print(f"Vocabulary generation error: {e}")
        return []


def _generate_practice_exercise(level: str, weak_skills: List[str] = None) -> dict:
    """Generate a single practice exercise based on student's weak areas."""
    if not openai_client:
        return {"type": "error", "content": "Practice exercises are currently unavailable."}

    focus_area = weak_skills[0] if weak_skills else "general"
    exercise_types = {
        "grammar": "sentence correction",
        "vocabulary": "fill-in-the-blank with synonyms",
        "fluency": "sentence building",
        "general": "mixed practice"
    }

    exercise_type = exercise_types.get(focus_area, "mixed practice")

    prompt = f"""Create a single {exercise_type} exercise for a {level} level English student.

Respond with a JSON object containing:
- "exercise": the exercise text/question
- "correct_answer": the expected answer
- "explanation": brief explanation of the correct answer
- "difficulty": "easy", "medium", or "hard"
- "skill_focus": what skill this targets

Make it challenging but appropriate for their level."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.8,
            response_format={"type": "json_object"},
        )
        import json as _json
        data = _json.loads(response.choices[0].message.content.strip())
        return data
    except Exception as e:
        return {"type": "error", "content": f"Could not generate exercise: {e}"}


def _lookup_word(word: str, level: str = "intermediate") -> dict:
    """Get definition and examples for a word using OpenAI."""
    if not openai_client:
        return {"definition": "Dictionary lookup is currently unavailable.", "examples": []}

    prompt = f"""Provide a dictionary entry for the word "{word}" appropriate for {level} level English learners.

Respond with a JSON object containing:
- "definition": clear, simple definition (1-2 sentences)
- "part_of_speech": noun/verb/adjective/etc.
- "examples": array of 2-3 natural example sentences
- "synonyms": array of 2-3 common synonyms (if applicable)
- "difficulty": "beginner", "intermediate", or "advanced"

Keep definitions simple and examples natural."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.3,  # Lower temperature for more consistent definitions
            response_format={"type": "json_object"},
        )
        import json as _json
        data = _json.loads(response.choices[0].message.content.strip())
        return data
    except Exception as e:
        return {"definition": f"Could not look up '{word}': {e}", "examples": []}


def _generate_grammar_examples(grammar_point: str, level: str) -> List[str]:
    """Generate example sentences for a specific grammar point."""
    if not openai_client:
        return ["Examples are currently unavailable."]

    prompt = f"""Generate 5 example sentences demonstrating "{grammar_point}" for {level} level English students.

Each sentence should:
- Be natural and appropriate for the level
- Clearly show the grammar point in use
- Include a variety of contexts

Respond with a JSON array of 5 sentences."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        import json as _json
        data = _json.loads(response.choices[0].message.content.strip())
        if isinstance(data, dict):
            return data.get("examples", data.get("sentences", [str(data)]))
        return data if isinstance(data, list) else ["Could not generate examples."]
    except Exception as e:
        return [f"Could not generate examples for '{grammar_point}': {e}"]


def _generate_personalized_tips(transcript_history: List[str], skill_scores: dict, level: str) -> str:
    """Generate personalized study tips based on student's performance."""
    if not openai_client:
        return "Personalized tips are currently unavailable."

    # Analyze patterns from recent transcripts and scores
    recent_transcripts = transcript_history[-5:] if transcript_history else []
    transcript_sample = " ".join(recent_transcripts)[:500]  # Limit length

    weak_skills = [k for k, v in skill_scores.items() if v > 0 and v < 3.5]
    strong_skills = [k for k, v in skill_scores.items() if v >= 4.0]

    prompt = f"""As an ESL teacher, analyze this {level} student's performance and give 3 specific, actionable study tips.

Recent transcript samples: "{transcript_sample}"
Skill scores (1-5 scale): {skill_scores}
Weak areas: {weak_skills}
Strong areas: {strong_skills}

Focus on the most impactful tips for their current level. Keep each tip to 1-2 sentences. Be encouraging and specific."""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=250,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return "Tips are currently unavailable. Keep practicing regularly!"


def _run_draft_feedback_and_score(transcript: str, level: str, task_script: str, example_answers: str = "") -> Tuple[str, dict]:
    """Generate AI feedback + skill scores using the task script and example answers as criteria.

    Returns (feedback_text, scores) where scores is a dict with keys:
    grammar, vocabulary, fluency (each 1-5 int).
    Pronunciation is intentionally omitted — filled in manually by the teacher.
    Falls back gracefully if JSON parsing fails.
    """
    _empty_scores = {"grammar": 0, "vocabulary": 0, "fluency": 0}
    if not openai_client:
        return "[AI feedback unavailable - set OPENAI_API_KEY]", _empty_scores
    try:
        import json as _json
        criteria = f'\nUse this task script as the criteria for feedback:\n"""\n{task_script}\n"""' if task_script else ""
        examples = f'\nReference these example answers for accuracy and completeness:\n"""\n{example_answers}\n"""' if example_answers else ""
        prompt = f"""You are an ESL teacher evaluating a {level} level student.

Student's transcript: "{transcript}"
{criteria}{examples}

Analyze the student's response according to CEFR {level} criteria:
- Grammar: Appropriate use of structures for level
- Vocabulary: Word choice and range appropriate for level
- Fluency: Natural flow and coherence

Identify ALL mistakes and errors made by the student (grammar, vocabulary, pronunciation hints, fluency issues).

Provide detailed, level-appropriate feedback that:
1. Notes what the student did well
2. Identifies specific mistakes and areas for improvement
3. Gives clear, actionable suggestions
4. References the task requirements and example answers when relevant
5. Encourages continued progress

Respond ONLY with a valid JSON object (no markdown, no extra text) in this exact format:
{{
  "feedback": "<80-120 word detailed feedback covering positives, specific mistakes found, improvements needed, and motivation>",
  "grammar": <1-5>,
  "vocabulary": <1-5>,
  "fluency": <1-5>
}}
Scores: 1=needs significant work (many errors), 2=developing (some errors), 3=acceptable (few errors), 4=good (minor errors), 5=excellent (few/no errors)."""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        data = _json.loads(raw)
        feedback = str(data.get("feedback", "")).strip() or raw
        scores = {
            "grammar": int(data.get("grammar") or 0),
            "vocabulary": int(data.get("vocabulary") or 0),
            "fluency": int(data.get("fluency") or 0),
        }
        return feedback, scores
    except Exception as e:
        return f"[GPT error: {e}]", _empty_scores


def _send_voice_result_to_chats(
    from_chat_id: int,
    message_id: int,
    info_text: str,
    send_to_work_chat: bool = True,
):
    """Forward voice and send info to admin chat; optionally to work chat too."""
    fwd_admin = bot.forward_message(ADMIN_FEEDBACK_CHAT_ID, from_chat_id, message_id)
    bot.send_message(
        ADMIN_FEEDBACK_CHAT_ID,
        info_text,
        reply_to_message_id=fwd_admin.message_id,
    )
    if send_to_work_chat:
        try:
            bot.forward_message(WORK_CHAT_ID, from_chat_id, message_id)
            bot.send_message(WORK_CHAT_ID, info_text)
        except Exception as e:
            print(f"Failed to send to work chat: {e}")


@bot.message_handler(content_types=["voice"])
def handle_voice(message: types.Message):
    chat_id = message.chat.id
    from_id = message.from_user.id if message.from_user else None

    # 1) Admin replying with voice to a forwarded student message → send feedback to student
    if chat_id == ADMIN_FEEDBACK_CHAT_ID and message.reply_to_message:
        replied_msg_id = message.reply_to_message.message_id
        fwd = getattr(message.reply_to_message, "forward_from", None)
        student_id = fwd.id if fwd else _student_reply_map.get(replied_msg_id)
        if student_id:
            try:
                bot.send_voice(
                    student_id,
                    message.voice.file_id,
                    caption="🎤 Personalized feedback from your teacher! Keep going!",
                )
                bot.reply_to(message, "✅ Voice feedback sent to student!")
            except Exception as e:
                bot.reply_to(message, f"Failed to send: {e}")
            return

    # 2) Admin test/practice: only when admin sends voice directly in their own bot DM (not work chat)
    is_admin_chat = chat_id == ADMIN_FEEDBACK_CHAT_ID
    is_reply_to_student = message.reply_to_message and (
        getattr(message.reply_to_message, "forward_from", None)
        or _student_reply_map.get(message.reply_to_message.message_id)
    )
    if is_admin_chat and from_id in ADMIN_IDS and not is_reply_to_student:
        level_label = "Practice (forwarded)" if (getattr(message, "forward_from", None) or getattr(message, "forward_date", None)) else "Test"
        temp_path = None
        try:
            file_info = bot.get_file(message.voice.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            temp_path = os.path.join(_BOT_DIR, "temp_reply.ogg")
            with open(temp_path, "wb") as f:
                f.write(downloaded_file)
            transcript, ai_draft = _run_transcribe_and_draft(temp_path, level_label)
            transcript_msg = (
                f"🧪 {level_label} voice\n"
                f"Transcript:\n{transcript}"
            )
            feedback_msg = ai_draft
            try:
                bot.forward_message(ADMIN_FEEDBACK_CHAT_ID, chat_id, message.message_id)
            except Exception:
                pass
            bot.send_message(ADMIN_FEEDBACK_CHAT_ID, transcript_msg)
            bot.send_message(ADMIN_FEEDBACK_CHAT_ID, feedback_msg)
            # Partner gets a copy in their DM
            try:
                bot.forward_message(PARTNER_CHAT_ID, chat_id, message.message_id)
                bot.send_message(PARTNER_CHAT_ID, transcript_msg)
                bot.send_message(PARTNER_CHAT_ID, feedback_msg)
            except Exception as e:
                print(f"Could not send practice result to partner: {e}")
            # Work chat (bot must be added to the group)
            try:
                bot.forward_message(WORK_CHAT_ID, chat_id, message.message_id)
                bot.send_message(WORK_CHAT_ID, transcript_msg)
                bot.send_message(WORK_CHAT_ID, feedback_msg)
            except Exception as e:
                print(f"Could not send to work chat (is the bot in the group?): {e}")
                bot.send_message(ADMIN_FEEDBACK_CHAT_ID, "⚠️ Couldn't post to work chat. Add the bot to the group if you haven't.")
        except Exception as e:
            print(f"Test voice error: {e}")
            bot.send_message(ADMIN_FEEDBACK_CHAT_ID, f"Test voice error: {e}")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return

    # 3) Student voice → transcript, script status, then clean AI draft (each as separate messages)
    if from_id in ADMIN_IDS:
        return

    temp_path = None
    transcript = "[Transcription unavailable - set OPENAI_API_KEY]"
    level = "Unknown"
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        temp_path = os.path.join(_BOT_DIR, "temp_reply.ogg")
        with open(temp_path, "wb") as f:
            f.write(downloaded_file)

        ws = get_students_worksheet()
        records = ws.get_all_records()
        for row in records:
            if str(row.get("Chat ID")) == str(chat_id):
                level = row.get("Level", "Unknown")
                break

        transcript, _ = _run_transcribe_and_draft(temp_path, level)

        # Message 1: forward voice + transcript
        transcript_msg = (
            f"🗣️ New voice reply\n"
            f"Student Chat ID: <code>{chat_id}</code> | Level: <b>{level}</b>\n\n"
            f"Transcript:\n{transcript}\n\n"
            f"Reply to the forwarded voice below with your voice or text to send feedback to the student."
        )
        fwd_admin = bot.forward_message(ADMIN_FEEDBACK_CHAT_ID, chat_id, message.message_id)
        _student_reply_map[fwd_admin.message_id] = chat_id
        transcript_sent = bot.send_message(ADMIN_FEEDBACK_CHAT_ID, transcript_msg, reply_to_message_id=fwd_admin.message_id)
        _student_reply_map[transcript_sent.message_id] = chat_id
        try:
            bot.forward_message(WORK_CHAT_ID, chat_id, message.message_id)
            bot.send_message(WORK_CHAT_ID, transcript_msg)
        except Exception as e:
            print(f"Failed to send to work chat: {e}")

        # Message 2: script status
        _, total_tasks_sent = get_student_level_and_total_tasks(chat_id)
        task_script = get_task_script(level, total_tasks_sent) if total_tasks_sent else ""
        print(f"[Voice] chat_id={chat_id} level={level} task#={total_tasks_sent} script_found={bool(task_script)}")
        script_status = (
            f"Task # used: <b>{total_tasks_sent}</b> | Script: {'✅ found' if task_script else '⚠️ not found (generic feedback used)'}"
        )
        status_sent = bot.send_message(ADMIN_FEEDBACK_CHAT_ID, script_status)
        _student_reply_map[status_sent.message_id] = chat_id
        try:
            bot.send_message(WORK_CHAT_ID, script_status)
        except Exception as e:
            print(f"Failed to send script status to work chat: {e}")

        # Message 3: clean AI draft (no header — ready to forward or copy-paste to student)
        example_answers = get_task_example_answers(level, total_tasks_sent) if total_tasks_sent else ""
        ai_draft, scores = _run_draft_feedback_and_score(transcript, level, task_script, example_answers)
        feedback_sent = bot.send_message(ADMIN_FEEDBACK_CHAT_ID, ai_draft)
        _student_reply_map[feedback_sent.message_id] = chat_id
        try:
            bot.send_message(WORK_CHAT_ID, ai_draft)
        except Exception as e:
            print(f"Failed to send feedback to work chat: {e}")

        # Log the reply + scores back to Task Log
        try:
            update_task_log_reply(chat_id, transcript, scores)
        except Exception as e:
            print(f"Failed to update task log reply: {e}")
    except Exception as e:
        print(f"Voice processing error: {e}")
        import traceback
        traceback.print_exc()
        try:
            bot.forward_message(ADMIN_FEEDBACK_CHAT_ID, chat_id, message.message_id)
            bot.send_message(
                ADMIN_FEEDBACK_CHAT_ID,
                f"Error (voice forwarded above): {e}",
            )
            try:
                bot.forward_message(WORK_CHAT_ID, chat_id, message.message_id)
                bot.send_message(WORK_CHAT_ID, f"Error: {e}")
            except Exception:
                pass
        except Exception:
            pass
    if temp_path and os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except OSError:
            pass

# =======================
# ADMIN TEXT REPLY → STUDENT
# =======================
@bot.message_handler(
    func=lambda m: (
        m.chat.id == ADMIN_FEEDBACK_CHAT_ID
        and m.reply_to_message is not None
        and _student_reply_map.get(m.reply_to_message.message_id) is not None
        and m.from_user is not None
        and m.from_user.id in ADMIN_IDS
        and m.content_type == "text"
    ),
    content_types=["text"],
)
def handle_admin_text_reply(message: types.Message):
    student_id = _student_reply_map.get(message.reply_to_message.message_id)
    if not student_id:
        return
    try:
        bot.send_message(student_id, message.text)
        bot.reply_to(message, "✅ Text feedback sent to student!")
    except Exception as e:
        bot.reply_to(message, f"Failed to send: {e}")


# =======================
# SCHEDULING
# =======================
def send_scheduled_tasks():
    """Send each student their *next* task (Total Tasks Sent + 1) and append to Task Log."""
    levels_to_send = ["A1", "A2", "B1", "B2"]
    sent_summary = []

    for level in levels_to_send:
        channel_id = LEVEL_CHANNELS.get(level)
        if not channel_id:
            continue
        students = get_students_by_level(level)
        if not students:
            continue
        sent_count = 0
        for chat_id in students:
            _, total = get_student_level_and_total_tasks(chat_id)
            next_task = total + 1
            message_id = get_channel_message_id_for_task(level, next_task)
            if message_id is None:
                print(f"Scheduled: no Task # {next_task} in {level} Task List for chat {chat_id}, skipping.")
                continue
            try:
                bot.forward_message(
                    chat_id=chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id,
                )
                script_text = get_task_script(level, next_task)
                if script_text:
                    bot.send_message(chat_id, script_text)
                student_name = get_student_name(chat_id)
                append_task_log(student_name, next_task, level, chat_id=chat_id)
                sent_count += 1
            except Exception as e:
                print(f"Failed to send to {chat_id}: {e}")
        sent_summary.append(f"{level}: {sent_count} students")

    if sent_summary:
        bot.send_message(ADMIN_FEEDBACK_CHAT_ID, "Scheduled tasks sent:\n" + "\n".join(sent_summary))
        try:
            bot.send_message(WORK_CHAT_ID, "Scheduled tasks sent:\n" + "\n".join(sent_summary))
        except Exception:
            pass

def send_monthly_progress_summaries():
    """On the 1st of each month, send every student their previous month's progress summary."""
    now = _now_moscow()
    if now.day != 1:
        return
    prev = now.replace(day=1) - timedelta(days=1)
    prev_year, prev_month = prev.year, prev.month
    # Month before that (for trend comparison)
    prev2 = prev.replace(day=1) - timedelta(days=1)
    prev2_year, prev2_month = prev2.year, prev2.month

    student_ids = _def_all_student_chat_ids()
    sent = 0
    for chat_id in student_ids:
        month_rows = _get_month_task_rows(chat_id, prev_year, prev_month)
        if not month_rows:
            continue
        prev2_rows = _get_month_task_rows(chat_id, prev2_year, prev2_month)
        try:
            text = _def_format_monthly_summary(chat_id, month_rows, prev2_rows)
            bot.send_message(chat_id, text)
            sent += 1
        except Exception as e:
            print(f"Monthly summary failed for {chat_id}: {e}")

    summary = f"Monthly progress summaries sent to {sent} student(s)."
    print(summary)
    try:
        bot.send_message(ADMIN_FEEDBACK_CHAT_ID, summary)
    except Exception:
        pass


# Schedule Mon, Wed, Fri at 06:00 UTC = 09:00 Moscow (UTC+3)
schedule.every().monday.at("06:00").do(send_scheduled_tasks)
schedule.every().wednesday.at("06:00").do(send_scheduled_tasks)
schedule.every().friday.at("06:00").do(send_scheduled_tasks)

# Monthly progress summaries on the 1st at 06:00 UTC = 09:00 Moscow
schedule.every().day.at("06:00").do(send_monthly_progress_summaries)

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)

# =======================
# MAIN
# =======================
def main():
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print("Bot is running...")
    try:
        bot.remove_webhook()
    except Exception as exc:
        print(f"Failed to remove webhook (continuing anyway): {exc}")
    bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    main()