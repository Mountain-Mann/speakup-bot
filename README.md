# SpeakUp Telegram Bot

Telegram bot using `pyTelegramBotAPI` (telebot) for ESL voice tasks:

- **Registration**: Students use `/start <level>`; `chat_id` and level are stored in Google Sheets.
- **Tasks**: Admins send tasks from a private channel to a level or to a single user.
- **Voice replies**: Student voice messages are transcribed (Whisper), get AI draft feedback (GPT), and are forwarded to the admin; the admin can reply with voice to send feedback back to the student.
- **Scheduling**: Optional automated task sends on Mon/Wed/Fri at 09:00 to configured levels.

## Setup

1. **Environment** (recommended: use a virtualenv):

   ```bash
   cd SpeakUp
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Telegram**: Create a bot with [BotFather](https://t.me/BotFather) and get the bot token.

3. **Config in `bot.py`** (or env vars where noted):

   - `BOT_TOKEN` — or set `TELEGRAM_BOT_TOKEN`
   - `ADMIN_IDS` — list of Telegram user IDs who can use admin commands
   - `TASK_SOURCE_CHANNEL_ID` — private channel ID where task messages live (e.g. `-1003530416415`)
   - `ADMIN_FEEDBACK_CHAT_ID` — chat where you receive voice replies and can reply with feedback
   - `SPREADSHEET_NAME` / `STUDENTS_SHEET_NAME` — Google Sheet name and worksheet name (e.g. `"SpeakUp!"` and `"Students1"`)

4. **Google Sheets**:

   - Create a Google Cloud project, enable **Google Sheets API** (and optionally **Google Drive API**).
   - Create a **service account**, download its JSON key, and save it as `service_account.json` in this folder.
   - Create a spreadsheet with the name and worksheet you set above; **share** that spreadsheet with the service account email (Editor). The bot will ensure a header row `chat_id | level` exists.

5. **OpenAI (optional)** — for voice transcription and AI draft feedback:

   - Set `OPENAI_API_KEY` (env var or in code). If unset, the bot still runs but skips transcription and AI draft; you’ll see a warning at startup.

6. **Run the bot**:

   ```bash
   export TELEGRAM_BOT_TOKEN="your_bot_token"   # optional if set in bot.py
   export OPENAI_API_KEY="sk-..."               # optional, for AI features
   python bot.py
   ```

   If a webhook was previously set, the bot removes it so polling works.

## Commands

### Students

- **`/start <level>`** — Register with a level (e.g. `A1`, `PARTNER`). Saves `chat_id` and level to the sheet. Students receive tasks in this chat.

### Admins (your user ID must be in `ADMIN_IDS`)

- **`/sendtask <level> <task_message_id>`** — Forward the message with that ID from the task channel to **all students** registered for that level.  
  Example: ` /sendtask A1 7`

- **`/sendtaskto <chat_id> <task_message_id>`** — Forward the task to **one specific chat** (e.g. for testing or a single partner).  
  Example: ` /sendtaskto 1253972975 7`

Task message IDs come from your channel links, e.g. `https://t.me/c/3530416415/7` → message ID is `7`.

## Voice flow

1. **Student sends a voice message** to the bot:
   - Bot downloads the audio, transcribes it with Whisper (if `OPENAI_API_KEY` is set).
   - Bot looks up the student’s level in the sheet and generates a short AI draft feedback with GPT (if API key set).
   - Bot forwards the original voice to your admin chat and sends a message with: student chat ID, level, transcript, and AI draft. You can **reply to that message with your own voice** to send feedback back to the student.

2. **You reply with voice** (in the admin chat, replying to the forwarded student message):
   - Bot sends your voice to the student’s chat with a short caption (e.g. “Personalized feedback from your teacher!”).

## Scheduling

The bot runs a scheduler that sends a task to configured levels on **Monday, Wednesday, and Friday at 09:00** (server time). In `bot.py`:

- `send_scheduled_tasks()` uses levels `["A1", "A2", "B1"]` and an `example_task_id` (default `123`). Change these and the task ID to match your channel and levels.
- A short summary of how many students received the task per level is sent to `ADMIN_FEEDBACK_CHAT_ID`.

## Files

- `bot.py` — main bot logic (config at top, then Sheets helpers, handlers, scheduler).
- `service_account.json` — Google service account key (do not commit; add to `.gitignore`).
- `requirements.txt` — `pyTelegramBotAPI`, `gspread`, `oauth2client`, `schedule`, `openai`.
