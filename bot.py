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


def _def_format_progress(chat_id: int) -> str:
    """Build the full /progress message for a student."""
    rows = get_student_task_log(chat_id)
    if not rows:
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
    exit(1)  # Stop if bot can't connect

print(f"Admin IDs: {ADMIN_IDS}")
print(f"Admin feedback chat: {ADMIN_FEEDBACK_CHAT_ID}")
print(f"Work chat: {WORK_CHAT_ID}")
print(f"Partner chat: {PARTNER_CHAT_ID}")
print(f"Level channels: {LEVEL_CHANNELS}")

# Maps message IDs in admin DM → student chat_id so reply detection
# works even when Telegram hides forward_from due to privacy settings.
_student_reply_map: dict = {}

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
# COMMAND: /progress (student-facing)
# =======================
@bot.message_handler(commands=["progress"])
def handle_progress(message: types.Message):
    chat_id = message.chat.id
    parts = message.text.strip().split()
    if len(parts) == 1:  # /progress — show own progress
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
# VOICE HANDLER (with AI transcription + draft)
# =======================
def _run_transcribe_and_draft(temp_path: str, level: str) -> tuple:
    """Returns (transcript, ai_draft)."""
    transcript = "[Transcription unavailable - set OPENAI_API_KEY]"
    ai_draft = "[AI feedback unavailable - set OPENAI_API_KEY]"
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
        try:
            prompt = f"""You are an ESL teacher. Student level: {level}
Transcript of their spoken response: "{transcript}"
Create short, encouraging feedback (60-100 words): positive comment, 1-2 improvements, end with motivation. Friendly tone."""
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.7,
            )
            ai_draft = response.choices[0].message.content.strip()
        except Exception as e:
            ai_draft = f"[GPT error: {e}]"
    return transcript, ai_draft


def _run_draft_feedback_and_score(transcript: str, level: str, task_script: str) -> Tuple[str, dict]:
    """Generate AI feedback + skill scores using the task script as criteria.

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
        prompt = f"""You are an ESL teacher. Student level: {level}.
Transcript of the student's spoken response: "{transcript}"
{criteria}
Respond ONLY with a valid JSON object (no markdown, no extra text) in this exact format:
{{
  "feedback": "<60-100 word encouraging feedback: something positive, 1-2 specific improvements, motivating close>",
  "grammar": <1-5>,
  "vocabulary": <1-5>,
  "fluency": <1-5>
}}
Scores: 1=needs a lot of work, 3=acceptable, 5=excellent."""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
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
        ai_draft, scores = _run_draft_feedback_and_score(transcript, level, task_script)
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