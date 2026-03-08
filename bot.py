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

# Telegram numeric user IDs for admins
ADMIN_IDS = [1253972975]

# Private channel that holds the tasks (e.g. -1001234567890)
TASK_SOURCE_CHANNEL_ID = -1003530416415

# Admin chat ID where you want to receive student voice replies
ADMIN_FEEDBACK_CHAT_ID = 1253972975

# Google Sheets configuration
GOOGLE_SERVICE_ACCOUNT_JSON = "service_account.json"
SPREADSHEET_NAME = "SpeakUp!"
STUDENTS_SHEET_NAME = "Students1"

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
    level = parts[1]
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
                from_chat_id=TASK_SOURCE_CHANNEL_ID,
                message_id=task_message_id,
            )
            sent_count += 1
        except Exception as exc:
            print(f"Failed to forward to {student_chat_id}: {exc}")
    bot.reply_to(
        message,
        f"Forwarded task <code>{task_message_id}</code> to {sent_count} student(s) of level <b>{level}</b>.",
    )

# =======================
# COMMAND: /sendtaskto (admin only, single user)
# =======================
@bot.message_handler(commands=["sendtaskto"])
def handle_sendtask_to(message: types.Message):
    """
    Usage (admin only):
    /sendtaskto <chat_id> <task_message_id>
    """
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "You are not authorized to use this command.")
        return
    parts = message.text.strip().split()
    if len(parts) != 3:
        bot.reply_to(
            message,
            "Usage: <code>/sendtaskto &lt;chat_id&gt; &lt;task_message_id&gt;</code>\n"
            "Example: <code>/sendtaskto 123456789 7</code>",
        )
        return
    try:
        target_chat_id = int(parts[1])
        task_message_id = int(parts[2])
    except ValueError:
        bot.reply_to(message, "Both chat_id and task_message_id must be integers.")
        return
    try:
        bot.forward_message(
            chat_id=target_chat_id,
            from_chat_id=TASK_SOURCE_CHANNEL_ID,
            message_id=task_message_id,
        )
    except Exception as exc:
        bot.reply_to(message, f"Failed to forward task: <code>{exc}</code>")
        return
    bot.reply_to(
        message,
        f"Forwarded task <code>{task_message_id}</code> to chat <code>{target_chat_id}</code>.",
    )

# =======================
# VOICE HANDLER (with AI transcription + draft)
# =======================
@bot.message_handler(content_types=["voice"])
def handle_voice(message: types.Message):
    chat_id = message.chat.id

    # If this is YOUR feedback reply → send to student
    if chat_id == ADMIN_FEEDBACK_CHAT_ID:
        if message.reply_to_message and message.reply_to_message.forward_from:
            student_id = message.reply_to_message.forward_from.id
            try:
                bot.send_voice(
                    student_id,
                    message.voice.file_id,
                    caption="🎤 Personalized feedback from your teacher! Keep going!"
                )
                bot.reply_to(message, "Feedback sent to student!")
            except Exception as e:
                bot.reply_to(message, f"Failed to send: {e}")
        return

    # Student voice reply → process & forward with AI
    temp_path = None
    transcript = "[Transcription unavailable - set OPENAI_API_KEY in .env or environment]"
    ai_draft = "[AI feedback unavailable - set OPENAI_API_KEY in .env or environment]"
    level = "Unknown"

    try:
        # Download audio
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        temp_path = "temp_reply.ogg"
        with open(temp_path, "wb") as f:
            f.write(downloaded_file)

        # Get student level (needed for AI draft)
        ws = get_students_worksheet()
        records = ws.get_all_records()
        for row in records:
            if str(row.get("chat_id")) == str(chat_id):
                level = row.get("level", "Unknown")
                break

        # Transcribe (Whisper)
        if openai_client:
            try:
                with open(temp_path, "rb") as audio_file:
                    transcript_resp = openai_client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="en"
                    )
                transcript = transcript_resp.text.strip() or "(empty)"
            except Exception as e:
                transcript = f"[Whisper error: {e}]"
        # else: keep placeholder

        # Generate AI draft (uses transcript)
        if openai_client:
            try:
                prompt = f"""
You are an ESL teacher. Student level: {level}
Transcript of their spoken response: "{transcript}"

Create short, encouraging feedback (60-100 words):
- Start with positive comment
- Mention 1-2 improvements (grammar, vocab, structure)
- End with motivation
Use friendly, supportive tone.
"""
                response = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=150,
                    temperature=0.7
                )
                ai_draft = response.choices[0].message.content.strip()
            except Exception as e:
                ai_draft = f"[GPT error: {e}]"
        # else: keep placeholder

        # Forward original voice to admin
        forwarded = bot.forward_message(
            ADMIN_FEEDBACK_CHAT_ID,
            chat_id,
            message.message_id
        )
        info_text = (
            f"🗣️ New voice reply to review\n"
            f"Student Chat ID: <code>{chat_id}</code>\n"
            f"Level: <b>{level}</b>\n"
            f"Transcript:\n{transcript}\n\n"
            f"AI Draft Feedback:\n{ai_draft}\n\n"
            f"Reply to this message with your voice (read draft + add pronunciation notes)."
        )
        bot.send_message(ADMIN_FEEDBACK_CHAT_ID, info_text, reply_to_message_id=forwarded.message_id)

    except Exception as e:
        print(f"Voice processing error: {e}")
        import traceback
        traceback.print_exc()
        try:
            bot.forward_message(ADMIN_FEEDBACK_CHAT_ID, chat_id, message.message_id)
            bot.send_message(
                ADMIN_FEEDBACK_CHAT_ID,
                f"Error before sending transcript/draft (voice forwarded above): {e}"
            )
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
    levels_to_send = ["A1", "A2", "B1"]  # ← customize your levels
    sent_summary = []

    for level in levels_to_send:
        students = get_students_by_level(level)
        if not students:
            continue

        # TODO: Replace with real rotation logic later (e.g. from Tasks sheet)
        example_task_id = 123  # ← CHANGE THIS! Use actual message ID or rotation

        sent_count = 0
        for chat_id in students:
            try:
                bot.forward_message(
                    chat_id=chat_id,
                    from_chat_id=TASK_SOURCE_CHANNEL_ID,
                    message_id=example_task_id,
                    caption=f"📢 New task for {level.upper()} level! Listen and reply with voice."
                )
                sent_count += 1
            except Exception as e:
                print(f"Failed to send to {chat_id}: {e}")

        sent_summary.append(f"{level}: {sent_count} students")

    if sent_summary:
        bot.send_message(ADMIN_FEEDBACK_CHAT_ID, f"Scheduled tasks sent:\n" + "\n".join(sent_summary))

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