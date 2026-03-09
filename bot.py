import os
import threading
import time
from typing import List
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

# Work chat group (you + partner); student voice results are also sent here
WORK_CHAT_ID = -5158365422

# Google Sheets configuration (path relative to this script so it works from any cwd)
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
GOOGLE_SERVICE_ACCOUNT_JSON = os.path.join(_BOT_DIR, "service_account.json")
SPREADSHEET_NAME = "SpeakUp!"
STUDENTS_SHEET_NAME = "Students1"

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
        ws = spreadsheet.add_worksheet(title=STUDENTS_SHEET_NAME, rows="1000", cols="3")
    values = ws.get_all_values()
    # Ensure header row exists and is correct
    expected_header = ["chat_id", "level"]
    if not values:
        ws.append_row(expected_header)
    else:
        first_row = [str(c).strip() for c in values[0]]
        # If header is missing or incorrect, overwrite row 1
        if first_row[:2] != expected_header:
            ws.update("A1:B1", [expected_header])
    return ws

def register_student(chat_id: int, level: str):
    ws = get_students_worksheet()
    all_rows = ws.get_all_records()
    row_index_to_update = None
    for idx, row in enumerate(all_rows, start=2):
        if str(row.get("chat_id")) == str(chat_id):
            row_index_to_update = idx
            break
    if row_index_to_update:
        ws.update_cell(row_index_to_update, 2, level)
    else:
        ws.append_row([str(chat_id), level])

def get_students_by_level(level: str) -> List[int]:
    ws = get_students_worksheet()
    all_rows = ws.get_all_records()
    chat_ids: List[int] = []
    for row in all_rows:
        if str(row.get("level")).strip().lower() == level.strip().lower():
            try:
                chat_ids.append(int(row.get("chat_id")))
            except (TypeError, ValueError):
                continue
    return chat_ids

# =======================
# TELEGRAM BOT SETUP
# =======================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# =======================
# COMMAND: /start
# =======================
@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    """
    Usage: /start <level>
    Example: /start A1
    """
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Hi! Please register with your level.\n"
            "Example: <code>/start A1</code>",
        )
        return
    level = parts[1].strip()
    register_student(message.chat.id, level)
    bot.reply_to(
        message,
        f"You are registered with level: <b>{level}</b>.\n"
        "You will receive tasks here.",
    )

# =======================
# COMMAND: /sendtask (admin only)
# =======================
@bot.message_handler(commands=["sendtask"])
def handle_sendtask(message: types.Message):
    """
    Usage (admin only):
    /sendtask <level> <task_message_id>
    """
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "You are not authorized to use this command.")
        return
    parts = message.text.strip().split()
    if len(parts) != 3:
        bot.reply_to(
            message,
            "Usage: <code>/sendtask &lt;level&gt; &lt;task_message_id&gt;</code>\n"
            "Example: <code>/sendtask A1 123</code>",
        )
        return
    level = parts[1].strip().upper()
    channel_id = LEVEL_CHANNELS.get(level)
    if not channel_id:
        bot.reply_to(
            message,
            f"Unknown level <b>{level}</b>. Use one of: A1, A2, B1, B2.",
        )
        return
    try:
        task_message_id = int(parts[2])
    except ValueError:
        bot.reply_to(message, "task_message_id must be an integer.")
        return
    students = get_students_by_level(level)
    if not students:
        bot.reply_to(message, f"No students found for level <b>{level}</b>.")
        return
    sent_count = 0
    for student_chat_id in students:
        try:
            bot.forward_message(
                chat_id=student_chat_id,
                from_chat_id=channel_id,
                message_id=task_message_id,
            )
            sent_count += 1
        except Exception as exc:
            print(f"Failed to forward to {student_chat_id}: {exc}")
    bot.reply_to(
        message,
        f"Forwarded task <code>{task_message_id}</code> from {level} library to {sent_count} student(s).",
    )

# =======================
# COMMAND: /sendtaskto (admin only, single user)
# =======================
@bot.message_handler(commands=["sendtaskto"])
def handle_sendtask_to(message: types.Message):
    """
    Usage (admin only):
    /sendtaskto <level> <chat_id> <task_message_id>
    Uses the task library channel for that level (A1, A2, B1, B2).
    """
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "You are not authorized to use this command.")
        return
    parts = message.text.strip().split()
    if len(parts) != 4:
        bot.reply_to(
            message,
            "Usage: <code>/sendtaskto &lt;level&gt; &lt;chat_id&gt; &lt;task_message_id&gt;</code>\n"
            "Example: <code>/sendtaskto A1 123456789 7</code>",
        )
        return
    level = parts[1].strip().upper()
    channel_id = LEVEL_CHANNELS.get(level)
    if not channel_id:
        bot.reply_to(message, f"Unknown level <b>{level}</b>. Use: A1, A2, B1, B2.")
        return
    try:
        target_chat_id = int(parts[2])
        task_message_id = int(parts[3])
    except ValueError:
        bot.reply_to(message, "chat_id and task_message_id must be integers.")
        return
    try:
        bot.forward_message(
            chat_id=target_chat_id,
            from_chat_id=channel_id,
            message_id=task_message_id,
        )
    except Exception as exc:
        bot.reply_to(message, f"Failed to forward task: <code>{exc}</code>")
        return
    bot.reply_to(
        message,
        f"Forwarded {level} task <code>{task_message_id}</code> to chat <code>{target_chat_id}</code>.",
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
                    model="whisper-1", file=audio_file, language="en"
                )
            transcript = transcript_resp.text.strip() or "(empty)"
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
            try:
                bot.forward_message(WORK_CHAT_ID, chat_id, message.message_id)
                bot.send_message(WORK_CHAT_ID, info_text)
            except Exception:
                pass
        except Exception as e:
            print(f"Test voice error: {e}")
            bot.send_message(ADMIN_FEEDBACK_CHAT_ID, f"Test voice error: {e}")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return

    # 3) Student voice → process and forward to admin + work chat
    temp_path = None
    transcript = "[Transcription unavailable - set OPENAI_API_KEY]"
    ai_draft = "[AI feedback unavailable - set OPENAI_API_KEY]"
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
            if str(row.get("chat_id")) == str(chat_id):
                level = row.get("level", "Unknown")
                break

        transcript, ai_draft = _run_transcribe_and_draft(temp_path, level)
        info_text = (
            f"🗣️ New voice reply to review\n"
            f"Student Chat ID: <code>{chat_id}</code>\n"
            f"Level: <b>{level}</b>\n"
            f"Transcript:\n{transcript}\n\n"
            f"AI Draft Feedback:\n{ai_draft}\n\n"
            f"Reply to the voice (in your bot DM) with your voice to send feedback to the student."
        )
        _send_voice_result_to_chats(chat_id, message.message_id, info_text)
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
    levels_to_send = ["A1", "A2", "B1", "B2"]
    sent_summary = []
    # TODO: per-level task IDs or rotation (e.g. from a sheet)
    example_task_ids = {"A1": 1, "A2": 1, "B1": 1, "B2": 1}

    for level in levels_to_send:
        channel_id = LEVEL_CHANNELS.get(level)
        if not channel_id:
            continue
        students = get_students_by_level(level)
        if not students:
            continue
        task_id = example_task_ids.get(level, 1)
        sent_count = 0
        for chat_id in students:
            try:
                bot.forward_message(
                    chat_id=chat_id,
                    from_chat_id=channel_id,
                    message_id=task_id,
                )
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