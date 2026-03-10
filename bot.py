import os
import threading
import time
from datetime import datetime
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
    "8534487614:AAFte69Q-FUOtSByRmatNI9IYvHhtrte3vs",
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set! AI features disabled.")
    openai_client = None
else:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Telegram numeric user IDs for admins (you + business partner)
ADMIN_IDS = [1253972975, 515525969]

# Level-specific task library channels (forward tasks from here by level)
LEVEL_CHANNELS = {
    "A1": -1003853572928,
    "A2": -1003790553224,
    "B1": -1003750480222,
    "B2": -1003530416415,
}

# Your DM with the bot (receive student voice replies + transcript/draft here)
ADMIN_FEEDBACK_CHAT_ID = 1253972975

# Work chat group (you + partner); student voice results are also sent here (bot must be in the group)
WORK_CHAT_ID = -5158365422
# Partner's DM (so they get practice/test results even if work chat fails)
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
TASK_LOG_HEADERS = ["Date Sent", "Student", "Task #", "Voice File Name", "Reply Received", "Week #", "Level"]

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


def append_task_log(student_name: str, task_number: int, level: str, voice_file_name: str = ""):
    """Append a row to Task Log so your Total Tasks Sent formula in Students updates."""
    ws = get_task_log_worksheet()
    now = datetime.utcnow()
    date_sent = now.strftime("%Y-%m-%d %H:%M")
    week_num = now.isocalendar()[1]
    ws.append_row([date_sent, student_name, task_number, voice_file_name or "", "", week_num, level])


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
                return (str(row.get("Script Text", row.get("Script text", "")) or "").strip()
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
            student_name = get_student_name(student_chat_id)
            append_task_log(student_name, next_task, level)
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
        student_name = get_student_name(target_chat_id)
        append_task_log(student_name, next_task, level)
    except Exception as exc:
        bot.reply_to(message, f"Failed to forward task: <code>{exc}</code>")
        return
    bot.reply_to(
        message,
        f"Sent {level} Task # <code>{next_task}</code> to chat <code>{target_chat_id}</code>. Task Log updated.",
    )

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


def _run_draft_feedback(transcript: str, level: str, task_script: str) -> str:
    """Generate AI feedback using the task script as criteria. Returns draft text."""
    if not openai_client:
        return "[AI feedback unavailable - set OPENAI_API_KEY]"
    try:
        criteria = f' Use this task script as the criteria for feedback:\n"""\n{task_script}\n"""' if task_script else ""
        prompt = f"""You are an ESL teacher. Student level: {level}.
Transcript of the student's spoken response: "{transcript}"
Create short, encouraging feedback (60-100 words): start with something positive, give 1-2 specific improvements based on the task criteria, end with motivation. Friendly tone.{criteria}"""
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[GPT error: {e}]"


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
        fwd = getattr(message.reply_to_message, "forward_from", None)
        if fwd:
            try:
                bot.send_voice(
                    fwd.id,
                    message.voice.file_id,
                    caption="🎤 Personalized feedback from your teacher! Keep going!",
                )
                bot.reply_to(message, "Feedback sent to student!")
            except Exception as e:
                bot.reply_to(message, f"Failed to send: {e}")
            return

    # 2) Admin test/practice: you (or partner) send a voice in your DM or work chat → transcribe + draft, send to both
    is_admin_chat = chat_id in (ADMIN_FEEDBACK_CHAT_ID, WORK_CHAT_ID)
    is_reply_to_student = message.reply_to_message and getattr(
        message.reply_to_message, "forward_from", None
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
            info_text = (
                f"🧪 {level_label} voice\n"
                f"Transcript:\n{transcript}\n\n"
                f"AI Draft Feedback:\n{ai_draft}"
            )
            try:
                bot.forward_message(ADMIN_FEEDBACK_CHAT_ID, chat_id, message.message_id)
            except Exception:
                pass
            bot.send_message(ADMIN_FEEDBACK_CHAT_ID, info_text)
            # Partner gets a copy in their DM
            try:
                bot.forward_message(PARTNER_CHAT_ID, chat_id, message.message_id)
                bot.send_message(PARTNER_CHAT_ID, info_text)
            except Exception as e:
                print(f"Could not send practice result to partner: {e}")
            # Work chat (bot must be added to the group)
            try:
                bot.forward_message(WORK_CHAT_ID, chat_id, message.message_id)
                bot.send_message(WORK_CHAT_ID, info_text)
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

    # 3) Student voice → transcript message first, then AI feedback (using task script) in a separate message
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

        # Message 1: forward voice + transcript only (no feedback yet)
        transcript_msg = (
            f"🗣️ New voice reply to review\n"
            f"Student Chat ID: <code>{chat_id}</code>\n"
            f"Level: <b>{level}</b>\n\n"
            f"Transcript:\n{transcript}\n\n"
            f"Reply to the voice (in your bot DM) with your voice to send feedback to the student."
        )
        fwd_admin = bot.forward_message(ADMIN_FEEDBACK_CHAT_ID, chat_id, message.message_id)
        bot.send_message(ADMIN_FEEDBACK_CHAT_ID, transcript_msg, reply_to_message_id=fwd_admin.message_id)
        try:
            bot.forward_message(WORK_CHAT_ID, chat_id, message.message_id)
            bot.send_message(WORK_CHAT_ID, transcript_msg)
        except Exception as e:
            print(f"Failed to send to work chat: {e}")

        # Message 2: AI feedback based on task script (Task # = Total Tasks Sent from Students)
        _, total_tasks_sent = get_student_level_and_total_tasks(chat_id)
        task_script = get_task_script(level, total_tasks_sent) if total_tasks_sent else ""
        ai_draft = _run_draft_feedback(transcript, level, task_script)
        feedback_msg = (
            f"📋 AI feedback (based on task criteria):\n\n{ai_draft}"
        )
        bot.send_message(ADMIN_FEEDBACK_CHAT_ID, feedback_msg)
        try:
            bot.send_message(WORK_CHAT_ID, feedback_msg)
        except Exception as e:
            print(f"Failed to send feedback to work chat: {e}")
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
                student_name = get_student_name(chat_id)
                append_task_log(student_name, next_task, level)
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

# Schedule Mon, Wed, Fri
schedule.every().monday.at("09:00").do(send_scheduled_tasks)
schedule.every().wednesday.at("09:00").do(send_scheduled_tasks)
schedule.every().friday.at("09:00").do(send_scheduled_tasks)

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