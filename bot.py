import logging
import os
import re
import time
import psycopg2
from psycopg2.extras import DictCursor
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# PostgreSQL Connection Settings
DB_CONFIG = {
    'dbname': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT')
}

BOT_TOKEN = os.getenv('BOT_TOKEN')

# Database Manager Class
class DatabaseManager:
    def __init__(self, max_retries=3, retry_delay=2):
        self.conn = None
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.connect_with_retry()

    def connect_with_retry(self):
        for attempt in range(self.max_retries):
            try:
                self.conn = psycopg2.connect(**DB_CONFIG)
                logger.info("✅ Connected to PostgreSQL database.")
                return
            except psycopg2.OperationalError as e:
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                raise
        logger.error("❌ Could not connect after retries.")
        raise ConnectionError("Database connection failed.")

    def execute_query(self, query, params=None, fetch=False):
        try:
            with self.conn.cursor(cursor_factory=DictCursor) as cursor:
                cursor.execute(query, params)
                if fetch:
                    return cursor.fetchall()
                self.conn.commit()
        except psycopg2.Error as e:
            logger.error(f"Database Error: {e}")
            self.conn.rollback()
            raise

    def get_daftars(self):
        query = """
            SELECT DISTINCT volume_number,
                   CASE volume_number
                       WHEN 'Дафтари аввал' THEN 1
                       WHEN 'Дафтари дуюм' THEN 2
                       WHEN 'Дафтари сеюм' THEN 3
                       WHEN 'Дафтари чорум' THEN 4
                       WHEN 'Дафтари панҷум' THEN 5
                   END AS daftar_order
            FROM poems 
            WHERE volume_number IN ('Дафтари аввал', 'Дафтари дуюм', 'Дафтари сеюм', 'Дафтари чорум', 'Дафтари панҷум')
            ORDER BY daftar_order
        """
        return self.execute_query(query, fetch=True) or []

    def get_all_daftars(self):
        return [
            {'volume_number': 'Дафтари аввал', 'available': True},
            {'volume_number': 'Дафтари дуюм', 'available': True},
            {'volume_number': 'Дафтари сеюм', 'available': True},
            {'volume_number': 'Дафтари чорум', 'available': True},
            {'volume_number': 'Дафтари панҷум', 'available': True},
            {'volume_number': 'Дафтари шашум', 'available': False}
        ]

    def get_poems_by_daftar(self, daftar_name):
        query = "SELECT poem_id, section_title FROM poems WHERE volume_number = %s ORDER BY poem_id"
        return self.execute_query(query, (daftar_name,), fetch=True) or []

    def search_poems(self, search_term):
        query = """
            SELECT poem_id, book_title, volume_number, section_title, poem_text
            FROM poems
            WHERE poem_tsv @@ plainto_tsquery('simple', %s)
            ORDER BY ts_rank(poem_tsv, plainto_tsquery('simple', %s)) DESC
            LIMIT 50
        """
        return self.execute_query(query, (search_term, search_term), fetch=True) or []

    def get_poem_by_id(self, poem_id):
        query = "SELECT * FROM poems WHERE poem_id = %s"
        result = self.execute_query(query, (poem_id,), fetch=True)
        return result[0] if result else None

    def close(self):
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed.")

db = DatabaseManager()

def highlight_text(text, search_term):
    if not search_term:
        return text
    try:
        safe_term = re.escape(search_term)
        return re.sub(f"({safe_term})", r"<b>\1</b>", text, flags=re.IGNORECASE)
    except Exception as e:
        logger.warning(f"Highlighting failed: {e}")
        return text

def split_long_message(text, max_length=4000):
    if len(text) <= max_length:
        return [text]
    parts = []
    while text:
        part = text[:max_length]
        last_line_break = part.rfind('\n')
        if last_line_break > max_length * 0.8:
            part = text[:last_line_break]
        parts.append(part)
        text = text[len(part):]
    return parts

async def send_message_safe(update_or_query, text, **kwargs):
    try:
        if isinstance(update_or_query, Update) and update_or_query.message:
            await update_or_query.message.reply_text(text, **kwargs)
        elif hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(text, **kwargs)
        elif hasattr(update_or_query, 'reply_text'):
            await update_or_query.reply_text(text, **kwargs)
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        if len(text) > 4000:
            parts = split_long_message(text)
            for part in parts:
                await send_message_safe(update_or_query, part, **kwargs)

# Start Command - Completely redesigned
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Маснавии Маънавӣ"],
        ["Девони Шамс"],
        ["Фиҳӣ Мо Фиҳ", "Маъолиҷи Сабъа"],
        ["Макотиб", "Саргузашт"],
        ["Ҷустуҷӯ"]
    ]
    await send_message_safe(
        update,
        "Асарҳои Балхӣ:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode='Markdown'
    )

async def send_poem(update_or_query, poem_id, show_full=False, part=0):
    poem = db.get_poem_by_id(poem_id)
    if not poem:
        await send_message_safe(update_or_query, "⚠️ Шеъри дархостшуда ёфт нашуд.")
        return

    intro = (
        f"📖 <b>{poem['book_title']}</b>\n"
        f"📜 <b>{poem['volume_number']} - {poem['section_title']}</b>\n"
    )
    
    poem_text = poem['poem_text']
    text_parts = split_long_message(poem_text)
    
    if show_full or len(text_parts) == 1:
        current_part = text_parts[part]
        message_text = f"{intro}<pre>{current_part}</pre>"
        
        keyboard = []
        if len(text_parts) > 1:
            if part > 0:
                keyboard.append(InlineKeyboardButton("⬅️ Қисми қаблӣ", callback_data=f"poem_{poem_id}_{part-1}"))
            if part < len(text_parts) - 1:
                keyboard.append(InlineKeyboardButton("Қисми баъдӣ ➡️", callback_data=f"poem_{poem_id}_{part+1}"))
        
        reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None
        
        try:
            if hasattr(update_or_query, 'edit_message_text'):
                await update_or_query.edit_message_text(
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
            else:
                await send_message_safe(
                    update_or_query,
                    message_text,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
        except Exception as e:
            logger.error(f"Error sending poem part: {e}")
            plain_text = f"{poem['book_title']}\n{poem['volume_number']} - {poem['section_title']}\n{current_part}"
            await send_message_safe(update_or_query, plain_text)
    else:
        preview_text = text_parts[0] + "\n\n... (шеър тӯлонӣ аст)"
        message_text = f"{intro}<pre>{preview_text}</pre>"
        
        keyboard = [[
            InlineKeyboardButton("📖 Дидани тамоми шеър", callback_data=f"full_{poem_id}_0")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await send_message_safe(
            update_or_query,
            message_text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Маснавии Маънавӣ":
        keyboard = [
            ["Дафтарҳои Маснавӣ"],
            ["Ба аввал"]
        ]
        await send_message_safe(
            update,
            "Маснавии Маънавӣ: Дастурро интихоб кунед",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

    elif text == "Дафтарҳои Маснавӣ":
        daftars = db.get_all_daftars()
        buttons = []
        for daftar in daftars:
            if daftar['available']:
                buttons.append([daftar['volume_number']])
            else:
                buttons.append([f"{daftar['volume_number']} (дастрас нест)"])
        
        buttons.append(["Ба аввал"])
        await send_message_safe(
            update,
            "Дафтарҳои Маснавӣ:",
            reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        )

    elif text in ["Дафтари аввал", "Дафтари дуюм", "Дафтари сеюм", "Дафтари чорум", "Дафтари панҷум"]:
        poems = db.get_poems_by_daftar(text)
        if not poems:
            await send_message_safe(update, f"❌ Шеър дар '{text}' ёфт нашуд.")
            return

        buttons = [[f"{poem['section_title']} (ID: {poem['poem_id']})"] for poem in poems]
        buttons.append(["Ба аввал"])
        await send_message_safe(
            update,
            f"📖 Шеърҳои {text}:",
            reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        )

    elif text == "Дафтари шашум":
        await send_message_safe(update, "Айни ҳол дастрас нест", reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Девони Шамс":
        description = "Девони Шамс:\nБахшҳо:\n- Ғазалиёт\n- Тарҷиот\n- Қасоид\n- Рубоиёт\n\nАйни ҳол дастрас нест"
        await send_message_safe(update, description, reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Фиҳӣ Мо Фиҳ":
        description = "Фиҳӣ Мо Фиҳ:\nНавъ: Наср\nШарҳ: Маҷмӯаи суҳбатҳо ва маърифатҳои ирфонӣ.\n\nАйни ҳол дастрас нест"
        await send_message_safe(update, description, reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Маъолиҷи Сабъа":
        description = "Маъолиҷи Сабъа:\nНавъ: Наср\nШарҳ: Ҳафт маҷлиси маърифатӣ ва иршодӣ аз Балхӣ.\n\nАйни ҳол дастрас нест"
        await send_message_safe(update, description, reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Макотиб":
        description = "Макотиб:\nНавъ: Номаҳо\nШарҳ: Маҷмӯаи номаҳои шахсии Балхӣ ба дӯстону муридон.\n\nАйни ҳол дастрас нест"
        await send_message_safe(update, description, reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Саргузашт":
        description = "Саргузашти Ҷалолуддини Балхӣ:\n\nАйни ҳол дастрас нест"
        await send_message_safe(update, description, reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Ҷустуҷӯ":
        await send_message_safe(
            update,
            "Лутфан калимаро пас аз /search ворид намоед. Масалан: /search ишқ ё /search бишнав аз най",
            reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True)
        )

    elif text == "Ба аввал":
        await start(update, context)

    elif "(ID:" in text:
        match = re.search(r'ID:\s*(\d+)', text)
        if match:
            poem_id = int(match.group(1))
            await send_poem(update, poem_id)
        else:
            await send_message_safe(update, "⚠️ Формати ID нодуруст аст.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if data.startswith("full_"):
        _, poem_id, part = data.split("_")
        await send_poem(query, int(poem_id), show_full=True, part=int(part))
    elif data.startswith("poem_"):
        _, poem_id, part = data.split("_")
        await send_poem(query, int(poem_id), show_full=True, part=int(part))

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
        text_parts = split_long_message(highlighted)
        
        intro = (
            f"📖 <b>{poem['book_title']}</b>\n"
            f"📜 <b>{poem['volume_number']} - {poem['section_title']}</b>\n"
        )
        
        for i, part in enumerate(text_parts):
            message_text = f"{intro}<pre>{part}</pre>"
            if i == len(text_parts) - 1:
                message_text += f"\n\nID: {poem['poem_id']}"
            await send_message_safe(update, message_text, parse_mode='HTML')

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.run_polling()

if __name__ == '__main__':
    main()
