import os
import re
import io
import threading
import requests
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# --- Flask Health Check ---
app = Flask(__name__)

@app.route('/')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

# --- Bot Setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("Error: BOT_TOKEN environment variable is not set.")
    exit(1)

user_states = {}
user_data = {}

def reset_user(chat_id):
    user_states[chat_id] = None
    user_data[chat_id] = {'links': {}, 'temp_url': None}

# --- Main Menu ---
async def send_main_menu(chat_id, context: ContextTypes.DEFAULT_TYPE):
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Add Link", callback_data="upload_file"),
         InlineKeyboardButton("🔍 Search", callback_data="search")],
        [InlineKeyboardButton("🗑 Delete", callback_data="delete")]
    ])
    await context.bot.send_message(chat_id, "📌 Choose an action:", reply_markup=markup)

# --- Start Command ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_user(update.effective_chat.id)
    await send_main_menu(update.effective_chat.id, context)

# --- Callback Handler ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "upload_file":
        user_states[chat_id] = 'awaiting_url'
        await context.bot.send_message(chat_id, "📤 Send me the file URL.")

    elif query.data == "search":
        links = user_data.get(chat_id, {}).get('links', {})
        if not links:
            await context.bot.send_message(chat_id, "⚠️ No links added yet.")
            await send_main_menu(chat_id, context)
            return
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Search one file", callback_data="search_one")],
            [InlineKeyboardButton("🔎 Search all files", callback_data="search_all")]
        ])
        await context.bot.send_message(chat_id, "Choose search mode:", reply_markup=markup)

    elif query.data == "search_one":
        await choose_file_for_search(chat_id, context)

    elif query.data == "search_all":
        user_states[chat_id] = "awaiting_domain_all"
        await context.bot.send_message(chat_id, "🔎 Send me the domain to search across all files.")

    elif query.data == "delete":
        links = user_data.get(chat_id, {}).get('links', {})
        if not links:
            await context.bot.send_message(chat_id, "⚠️ No links to delete.")
            await send_main_menu(chat_id, context)
        else:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🗑 {fname}", callback_data=f"delete_file:{fname}")]
                for fname in links.keys()
            ])
            await context.bot.send_message(chat_id, "Select a link to delete:", reply_markup=markup)

    elif query.data.startswith("delete_file:"):
        fname = query.data.split("delete_file:")[1]
        links = user_data.get(chat_id, {}).get('links', {})
        if fname in links:
            del links[fname]
            await context.bot.send_message(chat_id, f"✅ Link `{fname}` removed.", parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id, "⚠️ Link not found.")
        await send_main_menu(chat_id, context)

    elif query.data.startswith("search_file:"):
        fname = query.data.split("search_file:")[1]
        if fname in user_data[chat_id]['links']:
            user_states[chat_id] = f"awaiting_domain:{fname}"
            await context.bot.send_message(chat_id, f"🔍 Send me the domain to search in `{fname}`", parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id, "⚠️ Link not found.")
            await send_main_menu(chat_id, context)

# --- Upload Flow ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = user_states.get(chat_id)

    if state == 'awaiting_url':
        url = update.message.text.strip()
        if not url.startswith(('http://', 'https://')):
            await context.bot.send_message(chat_id, "⚠️ Invalid URL. Must start with http:// or https://")
            return
        user_data[chat_id]['temp_url'] = url
        user_states[chat_id] = 'awaiting_filename'
        await context.bot.send_message(chat_id, "✏️ What name do you want to give this file?")

    elif state == 'awaiting_filename':
        file_name = update.message.text.strip()
        if not file_name:
            await context.bot.send_message(chat_id, "⚠️ Name cannot be empty.")
            return
        url = user_data[chat_id].pop('temp_url', None)
        if not url:
            await context.bot.send_message(chat_id, "⚠️ No URL found.")
            await send_main_menu(chat_id, context)
            return
        user_data[chat_id]['links'][file_name] = url
        await context.bot.send_message(chat_id, f"✅ Link saved as `{file_name}`", parse_mode="Markdown")
        await send_main_menu(chat_id, context)

    elif state and state.startswith('awaiting_domain:'):
        fname = state.split("awaiting_domain:")[1]
        url = user_data[chat_id]['links'].get(fname)
        if not url:
            await context.bot.send_message(chat_id, "⚠️ Link not found.")
            await send_main_menu(chat_id, context)
            return
        target_domain = update.message.text.strip()
        await stream_search_with_live_progress(chat_id, context, url, target_domain, fname)

    elif state == "awaiting_domain_all":
        await handle_search_all(update, context)

# --- Search One File ---
async def choose_file_for_search(chat_id, context):
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔍 {fname}", callback_data=f"search_file:{fname}")]
        for fname in user_data[chat_id]['links'].keys()
    ])
    await context.bot.send_message(chat_id, "Select a link to search:", reply_markup=markup)

# --- Search All Files with Summary ---
async def handle_search_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target_domain = update.message.text.strip()
    links = user_data.get(chat_id, {}).get('links', {})
    if not links:
        await context.bot.send_message(chat_id, "⚠️ No files to search.")
        await send_main_menu(chat_id, context)
        return

    await context.bot.send_message(chat_id, f"🔎 Searching for `{target_domain}` across {len(links)} files...", parse_mode="Markdown")
    found_lines_stream = io.BytesIO()
    total_matches = 0
    match_counts = {}
    pattern = re.compile(r'\b' + re.escape(target_domain) + r'\b', re.IGNORECASE)

    for fname, url in links.items():
        match_counts[fname] = 0
        try:
            response = requests.get(url, stream=True, timeout=(10, 60))
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if line and pattern.search(line):
                    found_lines_stream.write(f"[{fname}] {line}\n".encode("utf-8"))
                    match_counts[fname] += 1
                    total_matches += 1
        except Exception as e:
            await context.bot.send_message(chat_id, f"⚠️ Error searching `{fname}`: {e}")

    summary_lines = [f"📊 Summary for `{target_domain}`:"]
    for fname, count in match_counts.items():
        summary_lines.append(f"- `{fname}`: {count} match{'es' if count != 1 else ''}")
    await context.bot.send_message(chat_id, "\n".join(summary_lines), parse_mode="Markdown")

    if total_matches > 0:
        found_lines_stream.seek(0)
        await context.bot.send_document(
            chat_id,
            document=found_lines_stream,
            filename=f"search_all_{target_domain}.txt",
            caption=f"✅ Found {total_matches} total matches across all files",
            parse_mode="Markdown"
        )
    else:
        await context.bot.send_message(chat_id, f"❌ No results for `{target_domain}` in any file.", parse_mode="Markdown")

    await send_main_menu(chat_id, context)

# --- Streaming Search
async def stream_search_with_live_progress(chat_id, context: ContextTypes.DEFAULT_TYPE, url, target_domain, fname):
    try:
        progress_msg = await context.bot.send_message(chat_id, "⏳ Starting search...")

        response = requests.get(url, stream=True, timeout=(10, 60))
        response.raise_for_status()

        total_bytes = int(response.headers.get('Content-Length', 0))
        bytes_read = 0
        lines_processed = 0
        found_lines_stream = io.BytesIO()
        found_lines_count = 0
        pattern = re.compile(r'\b' + re.escape(target_domain) + r'\b', re.IGNORECASE)
        last_percent = 0

        for chunk in response.iter_lines(decode_unicode=True):
            if not chunk:
                continue
            lines_processed += 1
            bytes_read += len(chunk.encode('utf-8')) + 1

            if pattern.search(chunk):
                found_lines_stream.write((chunk + "\n").encode("utf-8"))
                found_lines_count += 1

            if total_bytes > 0:
                percent = int((bytes_read / total_bytes) * 100)
                if percent >= last_percent + 5:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_msg.message_id,
                        text=f"📊 {percent}% done — found {found_lines_count}"
                    )
                    last_percent = percent
            else:
                if lines_processed % 5000 == 0:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=progress_msg.message_id,
                        text=f"📊 Processed {lines_processed:,} lines — found {found_lines_count}"
                    )

        # Final update
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"✅ Search complete — found {found_lines_count} matches"
        )

        if found_lines_count > 0:
            found_lines_stream.seek(0)
            await context.bot.send_document(
                chat_id,
                document=found_lines_stream,
                filename=f"search_results_{target_domain}.txt",
                caption=f"✅ Found {found_lines_count} matches for `{target_domain}` in `{fname}`",
                parse_mode="Markdown"
            )
        else:
            await context.bot.send_message(chat_id, f"❌ No results for `{target_domain}` in `{fname}`", parse_mode="Markdown")

    except Exception as e:
        await context.bot.send_message(chat_id, f"⚠️ Error: {e}")

    finally:
        await send_main_menu(chat_id, context)

# --- Run Bot + Flask ---
if __name__ == "__main__":
    print("🤖 Bot is running with Flask health check...")
    threading.Thread(target=run_flask).start()

    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(handle_callback))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app_bot.run_polling()
