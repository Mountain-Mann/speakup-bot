import os
import threading
import time
from typing import List

import telebot
from telebot import types

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import schedule


# =======================
# CONFIGURATION
# =======================

BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8534487614:AAFte69Q-FUOtSByRmatNI9IYvHhtrte3vs",
)

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
# FORWARD STUDENT VOICE REPLIES TO ADMIN
# =======================

@bot.message_handler(content_types=["voice"])
def handle_voice(message: types.Message):
    if message.chat.id == ADMIN_FEEDBACK_CHAT_ID:
        return

    try:
        bot.forward_message(
            chat_id=ADMIN_FEEDBACK_CHAT_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as exc:
        print(f"Failed to forward voice: {exc}")
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else ""
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    info = f"Voice reply from student\nChat ID: <code>{message.chat.id}</code>\n"
    if name:
        info += f"Name: {name}\n"
    if username:
        info += f"Username: {username}\n"

    bot.send_message(ADMIN_FEEDBACK_CHAT_ID, info)


# =======================
# BASIC SCHEDULING EXAMPLE
# =======================

def scheduled_task_example():
    """
    Example scheduled job:
    - Sends a reminder to all A1 students every day.
    """
    level = "A1"
    students = get_students_by_level(level)
    for chat_id in students:
        try:
            bot.send_message(
                chat_id,
                "Daily reminder: check today's task.",
            )
        except Exception as exc:
            print(f"Failed to send scheduled message to {chat_id}: {exc}")


def run_scheduler():
    schedule.every().day.at("09:00").do(scheduled_task_example)

    while True:
        schedule.run_pending()
        time.sleep(1)


# =======================
# MAIN
# =======================

def main():
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    print("Bot is running...")
    # Ensure polling can be used even if a webhook was previously set
    try:
        bot.remove_webhook()
    except Exception as exc:
        print(f"Failed to remove webhook (continuing anyway): {exc}")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()

