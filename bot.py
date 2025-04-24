import os
import logging
import psycopg2
import time
import re
import random
from datetime import date
from psycopg2 import sql
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from psycopg2.extras import DictCursor

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_USER_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(',')))

# ============================ DATABASE MANAGER ============================
class DatabaseManager:
    def __init__(self, max_retries=3, retry_delay=2):
        self.conn = None
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.connect_with_retry()
        self._ensure_database_integrity()

    def _ensure_database_integrity(self):
        try:
            if not self.execute_query("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'poems' AND column_name = 'unique_id'
            """, fetch=True):
                self.execute_query("ALTER TABLE poems ADD COLUMN unique_id SERIAL PRIMARY KEY")

            if not self.execute_query("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'highlighted_verses'
            """, fetch=True):
                self.execute_query("""
                    CREATE TABLE highlighted_verses (
                        id SERIAL PRIMARY KEY,
                        poem_unique_id INTEGER NOT NULL REFERENCES poems(unique_id),
                        verse_text TEXT NOT NULL
                    )
                """)
                if self.execute_query("SELECT 1 FROM poems LIMIT 1", fetch=True):
                    self.execute_query("""
                        INSERT INTO highlighted_verses (poem_unique_id, verse_text)
                        SELECT p.unique_id, p.poem_text FROM poems p
                        WHERE EXISTS (
                            SELECT 1 FROM poems p2 
                            WHERE p2.book_title = p.book_title
                            AND p2.volume_number = p.volume_number
                            AND p2.poem_id = p.poem_id
                            LIMIT 1
                        )
                    """)
            self.execute_query("""
                CREATE INDEX IF NOT EXISTS idx_poems_unique_id ON poems(unique_id);
                CREATE INDEX IF NOT EXISTS idx_hv_poem_unique_id ON highlighted_verses(poem_unique_id);
            """)
        except Exception as e:
            logger.error(f"DB Integrity Error: {e}")
            raise

    def connect_with_retry(self):
        for attempt in range(self.max_retries):
            try:
                self.conn = psycopg2.connect(DATABASE_URL, sslmode='require')
                logger.info("✅ Connected to DB")
                return
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
        raise ConnectionError("DB connection failed.")

    def execute_query(self, query, params=None, fetch=False):
        try:
            with self.conn.cursor(cursor_factory=DictCursor) as cursor:
                cursor.execute(query, params)
                if fetch:
                    return cursor.fetchall()
                self.conn.commit()
        except Exception as e:
            logger.error(f"DB Error: {e}")
            self.conn.rollback()
            raise

    def get_all_daftars(self):
        daftars = [
            {'volume_number': f'Дафтари {x}', 'volume_num': x}
            for x in ['аввал', 'дуюм', 'сеюм', 'чорум', 'панҷум', 'шашум']
        ]
        for daftar in daftars:
            res = self.execute_query("SELECT 1 FROM poems WHERE volume_number = %s LIMIT 1", (daftar['volume_number'],), True)
            daftar['available'] = bool(res)
        return daftars

    def get_poems_by_daftar(self, daftar_name):
        return self.execute_query("SELECT poem_id, section_title FROM poems WHERE volume_number = %s ORDER BY poem_id", (daftar_name,), True) or []

    def search_poems(self, term):
        q = """
            SELECT poem_id, book_title, volume_number, section_title, poem_text
            FROM poems WHERE poem_tsv @@ plainto_tsquery('simple', %s)
            ORDER BY ts_rank(poem_tsv, plainto_tsquery('simple', %s)) DESC LIMIT 50
        """
        return self.execute_query(q, (term, term), True)

    def get_poem_by_id(self, poem_id):
        res = self.execute_query("SELECT * FROM poems WHERE poem_id = %s", (poem_id,), True)
        return res[0] if res else None

    def get_poem_by_unique_id(self, unique_id):
        res = self.execute_query("SELECT * FROM poems WHERE unique_id = %s", (unique_id,), True)
        return res[0] if res else None

    def get_daily_verse(self):
        res = self.execute_query("""
            SELECT p.*, hv.verse_text FROM highlighted_verses hv
            JOIN poems p ON p.unique_id = hv.poem_unique_id
            ORDER BY RANDOM() LIMIT 1
        """, fetch=True)
        return res[0] if res else None

    def add_highlighted_verse(self, unique_id, text):
        self.execute_query("INSERT INTO highlighted_verses (poem_unique_id, verse_text) VALUES (%s, %s)", (unique_id, text))

    def delete_highlighted_verse(self, highlight_id):
        self.execute_query("DELETE FROM highlighted_verses WHERE id = %s", (highlight_id,))

    def is_highlight_exists(self, uid, text):
        q = "SELECT 1 FROM highlighted_verses WHERE poem_unique_id = %s AND verse_text = %s LIMIT 1"
        return bool(self.execute_query(q, (uid, text), True))

# ============================ UTILITY ============================
db = DatabaseManager()

def highlight_text(text, word):
    return re.sub(fr"({re.escape(word)})", r"<b>\1</b>", text, flags=re.IGNORECASE)

def split_long(text, maxlen=4000):
    if len(text) <= maxlen: return [text]
    parts, chunk = [], text
    while chunk:
        cut = chunk[:maxlen]
        split = cut.rfind('\n')
        if split > maxlen * 0.7: cut = chunk[:split]
        parts.append(cut)
        chunk = chunk[len(cut):]
    return parts

async def send_message_safe(update_or_query, text, **kwargs):
    if 'reply_markup' not in kwargs:
        kwargs['reply_markup'] = InlineKeyboardMarkup([[]])
    try:
        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(text, **kwargs)
        elif hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(text, **kwargs)
        elif hasattr(update_or_query, 'reply_text'):
            await update_or_query.reply_text(text, **kwargs)
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        for part in split_long(text):
            await send_message_safe(update_or_query, part, **kwargs)

# ============================ HANDLERS ============================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    try:
        if data.startswith("full_poem_"):
            unique_id = int(data.split("_")[2])
            poem = db.get_poem_by_unique_id(unique_id)
            if poem:
                await send_poem(query, poem['poem_id'], show_full=True)

        elif data.startswith("poem_"):
            parts = data.split("_")
            poem_id = int(parts[1])
            part = int(parts[2]) if len(parts) > 2 else 0
            await send_poem(query, poem_id, show_full=True, part=part)

        elif data.startswith("back_to_daily_"):
            poem_id = int(data.split("_")[3])
            verse = db.execute_query(
                """
                SELECT p.*, hv.verse_text FROM highlighted_verses hv
                JOIN poems p ON p.unique_id = hv.poem_unique_id
                WHERE p.poem_id = %s
                """,
                (poem_id,), fetch=True
            )
            if verse:
                message_text = (
                    f"🌟 <b>Мисраи рӯз</b> 🌟\n\n"
                    f"📖 <b>{verse['book_title']}</b>\n"
                    f"📜 <b>{verse['volume_number']} - Бахши {verse['poem_id']}</b>\n\n"
                    f"<i>{verse['verse_text']}</i>"
                )

                keyboard = [[
                    InlineKeyboardButton("📖 Шеъри пурра", callback_data=f"full_poem_{verse[0]['unique_id']}")
                ]]
                await query.edit_message_text(
                    text=message_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        elif data == "back_to_start":
            await start(query, context)

    except Exception as e:
        logger.error(f"Error in button_callback: {e}")
        await query.answer("Хатогӣ рух дод. Боз кӯшиш кунед.")

async def highlight_verse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔️ Шумо иҷозати иҷрои ин фармонро надоред.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Истифода: /highlight <unique_id> <матни мисра>")
        return
    try:
        poem_unique_id = int(context.args[0])
        verse_text = ' '.join(context.args[1:]).replace('||', '
')
        if db.is_highlight_exists(poem_unique_id, verse_text):
            await update.message.reply_text("⚠️ Ин мисра аллакай мавҷуд аст.")
            return
        db.add_highlighted_verse(poem_unique_id, verse_text)
        await update.message.reply_text(f"✅ Мисра илова шуд:

<pre>{verse_text}</pre>", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error adding highlight: {e}")
        await update.message.reply_text("❌ Хатогӣ дар иловаи мисра.")

async def delete_highlight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔️ Шумо иҷозати иҷрои ин фармонро надоред.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Истифода: /delete_highlight <highlight_id>")
        return
    try:
        highlight_id = int(context.args[0])
        db.delete_highlighted_verse(highlight_id)
        await update.message.reply_text(f"✅ Мисраи бо ID {highlight_id} ҳазф шуд.")
    except Exception as e:
        logger.error(f"Error deleting highlight: {e}")
        await update.message.reply_text("❌ Хатогӣ дар ҳазф.")

async def send_poem(update_or_query, poem_id, show_full=False, part=0, search_term=""):
    poem = db.get_poem_by_id(poem_id)
    if not poem:
        await send_message_safe(update_or_query, "⚠️ Шеъри дархостшуда ёфт нашуд.")
        return
    intro = (
        f"📖 <b>{poem['book_title']}</b>
"
        f"📜 <b>{poem['volume_number']} - Бахши {poem['poem_id']}</b>
"
        f"🔹 {poem['section_title']}

"
    )
    poem_text = poem['poem_text']
    if search_term:
        poem_text = highlight_text(poem_text, search_term)
    text_parts = split_long(poem_text)
    if show_full or len(text_parts) == 1:
        current_part = text_parts[part]
        message_text = f"{intro}<pre>{current_part}</pre>"
        nav_buttons = []
        if part > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Қисми қаблӣ", callback_data=f"poem_{poem_id}_{part-1}"))
        if part < len(text_parts) - 1:
            nav_buttons.append(InlineKeyboardButton("Қисми баъдӣ ➡️", callback_data=f"poem_{poem_id}_{part+1}"))
        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🏠 Ба аввал", callback_data="back_to_start")])
        await send_message_safe(
            update_or_query,
            message_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        preview_text = text_parts[0] + "

... (шеър тӯлонӣ аст)"
        message_text = f"{intro}<pre>{preview_text}</pre>"
        keyboard = [[InlineKeyboardButton("📖 Шеъри пурра", callback_data=f"poem_{poem_id}_0")]]
        await send_message_safe(update_or_query, message_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
from telegram.constants import ParseMode

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Маснавии Маънавӣ"],
        ["Девони Шамс"],
        ["Ҷустуҷӯ", "Маълумот дар бораи Балхӣ"],
        ["Мисраи рӯз"]
    ]
    await send_message_safe(
        update,
        "Асарҳои Мавлоно Ҷалолуддини Балхӣ. Лутфан аз рӯйи тугмаҳои зер интихоб кунед:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )

async def handle_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_message_safe(
        update,
        "Лутфан аз тугмаҳои меню истифода баред ё бо фармони /search ҷустуҷӯ кунед.",
        reply_markup=ReplyKeyboardMarkup([["🏠 Ба аввал"]], resize_keyboard=True)
    )

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search_term = ' '.join(context.args).strip()
    if not search_term:
        await send_message_safe(update, "⚠️ Лутфан калима ё мисраро барои ҷустуҷӯ ворид кунед.")
        return
    poems = db.search_poems(search_term)
    if not poems:
        await send_message_safe(update, f"⚠️ Ҳеҷ шеъре барои '{search_term}' ёфт нашуд.")
        return
    for poem in poems:
        highlighted = highlight_text(poem['poem_text'], search_term)
        text_parts = split_long(highlighted)
        intro = (
            f"📖 <b>{poem['book_title']}</b>
"
            f"📜 <b>{poem['volume_number']} - Бахши {poem['poem_id']}</b>
"
            f"🔹 {poem['section_title']}

"
        )
        for part in text_parts:
            message_text = f"{intro}<pre>{part}</pre>"
            await send_message_safe(update, message_text, parse_mode=ParseMode.HTML)

async def daily_verse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    verse = db.get_daily_verse()
    if not verse:
        await send_message_safe(update, "⚠️ Мисраи рӯз ёфт нашуд.")
        return
    message_text = (
                    f"🌟 <b>Мисраи рӯз</b> 🌟\n\n"
                    f"📖 <b>{verse['book_title']}</b>\n"
                    f"📜 <b>{verse['volume_number']} - Бахши {verse['poem_id']}</b>\n\n"
                    f"<i>{verse['verse_text']}</i>"
                )
    keyboard = [[
        InlineKeyboardButton("📖 Шеъри пурра", callback_data=f"full_poem_{verse['unique_id']}")
    ]]
    await send_message_safe(update, message_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
# Command and callback handlers will be reinserted next (e.g. /start, /search, button_callback, etc.)

# ============================ MAIN ============================
def main():
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Environment variables not set!")
        return
    app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("daily", daily_verse))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_invalid_input))
        app.add_handler(CommandHandler("highlight", highlight_verse))
    app.add_handler(CommandHandler("delete_highlight", delete_highlight))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()

if __name__ == '__main__':
    main()
