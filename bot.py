import os
import logging
import psycopg2
import time as time_module
import re
import random
from datetime import date, time
from psycopg2 import sql
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup
from itertools import zip_longest
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from psycopg2.extras import DictCursor
from dotenv import load_dotenv
import pandas as pd
from io import BytesIO
import telegram

# Load environment variables
load_dotenv()

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get environment variables
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_IDS = os.getenv('ADMIN_IDS', '')
ADMIN_USER_IDS = [int(id) for id in ADMIN_IDS.split(',')] if ADMIN_IDS else []
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')

# Validate required environment variables
if not all([BOT_TOKEN, DATABASE_URL, TELEGRAM_CHANNEL_ID]):
    logger.error("Missing required environment variables!")
    raise ValueError("Missing required environment variables. Please check your .env file.")

# Database Manager Class
class DatabaseManager:
    def __init__(self, max_retries=3, retry_delay=2):
        self.conn = None
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.connect_with_retry()
        self._ensure_database_integrity()

    def connect_with_retry(self):
        """Connect to the database with retry logic"""
        for attempt in range(self.max_retries):
            try:
                self.conn = psycopg2.connect(DATABASE_URL, sslmode='require')
                logger.info("✅ Connected to PostgreSQL database.")
                return
            except psycopg2.OperationalError as e:
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt < self.max_retries - 1:
                    time_module.sleep(self.retry_delay)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                raise
        logger.error("❌ Could not connect after retries.")
        raise ConnectionError("Database connection failed.")

    def execute_query(self, query, params=None, fetch=False):
        """Execute a database query with error handling"""
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

    def _ensure_database_integrity(self):
        """Ensure all required database structure exists"""
        try:
            # Create divan_poems table if not exists
            if not self.execute_query("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'divan_poems'
                """, fetch=True):
                
                self.execute_query("""
                CREATE TABLE divan_poems (
                    id SERIAL PRIMARY KEY,
                    poem_id INTEGER NOT NULL,
                    section_title TEXT NOT NULL,
                    poem_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(poem_id)
                )
                """)
                logger.info("Created divan_poems table")
                
                # Add search capability
                self.execute_query("""
                ALTER TABLE divan_poems 
                ADD COLUMN poem_tsv tsvector 
                GENERATED ALWAYS AS (to_tsvector('simple', poem_text)) STORED
                """)
                
                # Add index for text search
                self.execute_query("""
                CREATE INDEX idx_divan_poems_tsv ON divan_poems USING gin(poem_tsv)
                """)
                
            # Create poem_history table if not exists
            if not self.execute_query("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'poem_history'
                """, fetch=True):
                
                self.execute_query("""
                CREATE TABLE poem_history (
                    id SERIAL PRIMARY KEY,
                    poem_id INTEGER NOT NULL REFERENCES mixed_poems(id),
                    date_shown DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(poem_id, date_shown)
                )
                """)
                logger.info("Created poem_history table")
                
            # Create poems table if not exists
            if not self.execute_query("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'poems'
                """, fetch=True):
                
                self.execute_query("""
                CREATE TABLE poems (
                    id SERIAL PRIMARY KEY,
                    unique_id SERIAL UNIQUE,
                    book_title TEXT NOT NULL,
                    volume_number TEXT NOT NULL,
                    poem_id INTEGER NOT NULL,
                    section_title TEXT NOT NULL,
                    poem_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(book_title, volume_number, poem_id)
                )
                """)
                logger.info("Created poems table")
                
                # Add search capability
                self.execute_query("""
                ALTER TABLE poems 
                ADD COLUMN poem_tsv tsvector 
                GENERATED ALWAYS AS (to_tsvector('simple', poem_text)) STORED
                """)
                
                # Add index for text search
                self.execute_query("""
                CREATE INDEX idx_poems_tsv ON poems USING gin(poem_tsv)
                """)

            # Create highlighted_verses table if not exists
            if not self.execute_query("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'highlighted_verses'
                """, fetch=True):
                
                self.execute_query("""
                CREATE TABLE highlighted_verses (
                    id SERIAL PRIMARY KEY,
                    poem_unique_id INTEGER NOT NULL REFERENCES poems(unique_id),
                    verse_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                logger.info("Created highlighted_verses table")

            # Create mixed_poems table if not exists
            if not self.execute_query("""
                SELECT table_name FROM information_schema.tables 
                WHERE table_name = 'mixed_poems'
                """, fetch=True):
                
                self.execute_query("""
                CREATE TABLE mixed_poems (
                    id SERIAL PRIMARY KEY,
                    poem_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
                logger.info("Created mixed_poems table")

        except Exception as e:
            logger.error(f"Error ensuring database integrity: {e}")
            raise

    def get_divan_ghazals(self, page=1, per_page=10):
        """Get paginated ghazals from Divan-e Shams"""
        offset = (page - 1) * per_page
        query = """
        SELECT poem_id, section_title, poem_text 
        FROM divan_poems 
        ORDER BY poem_id
        LIMIT %s OFFSET %s
        """
        ghazals = self.execute_query(query, (per_page, offset), fetch=True) or []
        
        # Get total count
        count_query = """
        SELECT COUNT(*) FROM divan_poems
        """
        count = self.execute_query(count_query, fetch=True)[0][0]
        
        return ghazals, count

    def get_divan_ghazal_by_id(self, ghazal_id):
        """Get a specific ghazal from Divan-e Shams"""
        query = """
        SELECT * FROM divan_poems 
        WHERE poem_id = %s
        """
        result = self.execute_query(query, (ghazal_id,), fetch=True)
        return result[0] if result else None

    def add_divan_poem(self, section_title, poem_text):
        """Add a new ghazal to Divan-e Shams"""
        # Extract poem_id from section title (e.g., "Ғазали 24" -> 24)
        import re
        match = re.search(r'Ғазали\s*(\d+)', section_title)
        if not match:
            raise ValueError("Invalid section title format. Expected 'Ғазали X' where X is a number.")
        
        poem_id = int(match.group(1))
        
        # Check if poem already exists
        check_query = """
        SELECT EXISTS(
            SELECT 1 FROM divan_poems 
            WHERE poem_id = %s
        )
        """
        result = self.execute_query(check_query, (poem_id,), fetch=True)
        exists = result[0][0] if result else False
        
        if exists:
            return False, "Ғазал бо ин рақам аллакай мавҷуд аст."
        
        # Insert new poem
        insert_query = """
        INSERT INTO divan_poems (poem_id, section_title, poem_text)
        VALUES (%s, %s, %s)
        """
        self.execute_query(insert_query, (poem_id, section_title, poem_text))
        return True, f"Ғазали {poem_id} бомуваффақият илова шуд."

    def get_all_daftars(self):
        daftars = [
            {'volume_number': 'Дафтари аввал', 'volume_num': 1},
            {'volume_number': 'Дафтари дуюм', 'volume_num': 2},
            {'volume_number': 'Дафтари сеюм', 'volume_num': 3},
            {'volume_number': 'Дафтари чорум', 'volume_num': 4},
            {'volume_number': 'Дафтари панҷум', 'volume_num': 5},
            {'volume_number': 'Дафтари шашум', 'volume_num': 6}
        ]

        # Check which daftars have poems in DB
        for daftar in daftars:
            query = """
                SELECT EXISTS (
                SELECT 1 FROM poems 
                WHERE volume_number = %s 
                LIMIT 1
            )
            """
            result = self.execute_query(query, (daftar['volume_number'],), fetch=True)
            daftar['available'] = result[0][0] if result else False

        return daftars

    def get_poems_by_daftar(self, daftar_name):
        query = """
        SELECT poem_id, section_title 
        FROM poems 
        WHERE volume_number = %s 
        ORDER BY poem_id
        """
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

    def get_poem_by_id(self, poem_id, volume_number=None):
        query = "SELECT * FROM poems WHERE poem_id = %s"
        if volume_number:
            query = sql.SQL("SELECT * FROM poems WHERE poem_id = %s AND volume_number = %s").format(sql.Literal(poem_id), sql.Literal(volume_number))
        result = self.execute_query(query, (poem_id,) if not volume_number else (poem_id, volume_number), fetch=True)
        return result[0] if result else None

    def get_daily_verse(self):
        query = """
        SELECT p.*, hv.verse_text
        FROM highlighted_verses hv
        JOIN poems p ON p.unique_id = hv.poem_unique_id
        ORDER BY RANDOM()
        LIMIT 1
        """
        result = self.execute_query(query, fetch=True)
        return result[0] if result else None

    def add_highlighted_verse(self, poem_unique_id, verse_text):
        query = """
        INSERT INTO highlighted_verses (poem_unique_id, verse_text)
        VALUES (%s, %s)
        """
        self.execute_query(query, (poem_unique_id, verse_text))

    def is_highlight_exists(self, poem_unique_id, verse_text):
        query = """
        SELECT 1 FROM highlighted_verses 
        WHERE poem_unique_id = %s AND verse_text = %s
        LIMIT 1
        """
        return bool(self.execute_query(query, (poem_unique_id, verse_text), fetch=True))

    def delete_highlighted_verse(self, highlight_id):
        query = "DELETE FROM highlighted_verses WHERE verse_id = %s"
        self.execute_query(query, (highlight_id,))

    def close(self):
        """Close the database connection"""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed.")

    def get_random_mixed_poem(self):
        query = "SELECT * FROM mixed_poems ORDER BY RANDOM() LIMIT 1"
        result = self.execute_query(query, fetch=True)
        return result[0] if result else None

    def add_mixed_poem(self, poem_text):
        # First check if poem exists
        check_query = "SELECT EXISTS(SELECT 1 FROM mixed_poems WHERE poem_text = %s)"
        result = self.execute_query(check_query, (poem_text,), fetch=True)
        exists = result[0][0] if result else False
        
        if exists:
            return False
            
        # If poem doesn't exist, insert it
        query = "INSERT INTO mixed_poems (poem_text) VALUES (%s)"
        self.execute_query(query, (poem_text,))
        return True

    def add_divan_poem(self, section_title, poem_text):
        # Extract poem_id from section title (e.g., "Ғазали 24" -> 24)
        import re
        match = re.search(r'Ғазали\s*(\d+)', section_title)
        if not match:
            raise ValueError("Invalid section title format. Expected 'Ғазали X' where X is a number.")
        
        poem_id = int(match.group(1))
        
        # Check if poem already exists
        check_query = """
        SELECT EXISTS(
            SELECT 1 FROM poems 
            WHERE book_title = 'Девони Шамс' 
            AND poem_id = %s
        )
        """
        result = self.execute_query(check_query, (poem_id,), fetch=True)
        exists = result[0][0] if result else False
        
        if exists:
            return False, "Ғазал бо ин рақам аллакай мавҷуд аст."
        
        # Insert new poem
        insert_query = """
        INSERT INTO poems (book_title, section_title, poem_id, poem_text)
        VALUES ('Девони Шамс', %s, %s, %s)
        """
        self.execute_query(insert_query, (section_title, poem_id, poem_text))
        return True, f"Ғазали {poem_id} бомуваффақият илова шуд."


# Initialize database connection
db = DatabaseManager()

# Utility functions
def highlight_text(text, search_term):
    if not search_term:
        return text
    try:
        words = search_term.split()
        result = text
        for word in words:
            safe_term = re.escape(word)
            result = re.sub(f"({safe_term})", r'<b>\1</b>', result, flags=re.IGNORECASE)
        return result
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

# ================== COMMAND HANDLERS ==================
async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Bypass check for admin users
    if user_id in ADMIN_USER_IDS:
        return True
        
    channel_id = TELEGRAM_CHANNEL_ID
    
    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking subscription: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [
            InlineKeyboardButton("📚 Маснавии Маънавӣ", callback_data="masnavi_info"),
            InlineKeyboardButton("📖 Девони Шамс", callback_data="divan_info")
        ],
        [
            InlineKeyboardButton("🎲 Шеърҳои тасодуфӣ", callback_data="random_poem"),
            InlineKeyboardButton("⭐️ Мисраи рӯз", callback_data="daily_verse")
        ],
        [
            InlineKeyboardButton("ℹ️ Дар бораи Балхӣ", url="https://telegra.ph/Mavlonoi-Balh-04-23")
        ]
    ]

    welcome_text = (
        "━━━━━ 🌟 <b>Хуш омадед</b> 🌟 ━━━━━\n\n"
        "<b>Ин ҷо шумо метавонед:</b>\n\n"
        "📚 Маснавии Маънавиро мутолиа кунед\n"
        "📖 Девони Шамсро хонед\n"
        "🎲 Шеърҳои тасодуфиро бубинед (барои истифодаи ин хусусият лутфан ба канали мо обуна шавед)\n"
        "⭐️ Мисраҳои рӯзро бубинед\n"
        "🔍 Ва ҷустуҷӯи осорро анҷам диҳед\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Лутфан интихоб кунед:</i>"
    )

    if isinstance(update, Update) and update.callback_query:
        await update.callback_query.edit_message_text(
            text=welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            text=welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )

async def balkhi_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Short intro message
    info_text = "📖 <b>Маълумот дар бораи Мавлоно Ҷалолуддини Балхӣ</b>\n\nБарои хондани тарҷумаи ҳол ва осораш, тугмаи зерро пахш кунед:"

    # Keyboard with Telegraph button
    keyboard = [
        [InlineKeyboardButton("📜 Маълумот дар Telegra.ph", url="https://telegra.ph/Mavlonoi-Balh-04-23")],  # Replace with your link
        [InlineKeyboardButton("Маснавии Маънавӣ", callback_data="masnavi_info")],
        [InlineKeyboardButton("Девони Шамс", callback_data="divan_info")],
        [InlineKeyboardButton("🏠 Ба аввал", callback_data="back_to_start")]
    ]

    await send_message_safe(
        update,
        info_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def masnavi_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    daftars = db.get_all_daftars()  # Now returns dynamic availability
    buttons = []
    for daftar in daftars:
        if daftar['available']:
            buttons.append([InlineKeyboardButton(
                daftar['volume_number'], 
                callback_data=f"daftar_{daftar['volume_number']}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                f"{daftar['volume_number']} (дастрас нест)", 
                callback_data="unavailable_daftar"
            )])

    buttons.append([InlineKeyboardButton("Ба аввал", callback_data="back_to_start")])

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            text="Дафтарҳои Маснавӣ:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await send_message_safe(
            update,
            "Дафтарҳои Маснавӣ:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

async def show_poems_page(update: Update, context: ContextTypes.DEFAULT_TYPE, daftar_name: str, page: int = 1):
    poems, total = [], 0
    try:
        poems = db.get_poems_by_daftar(daftar_name)
        total = len(poems)
    except Exception as e:
        logger.error(f"Error getting poems: {e}")
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.answer("Хатогӣ дар гирифтани рӯйхати шеърҳо", show_alert=True)
        return

    if not poems:
        message = f"❌ Шеър дар '{daftar_name}' ёфт нашуд."
        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.edit_message_text(
                text=message,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Бозгашт", callback_data="back_to_daftars")
                ]])
            )
        else:
            await send_message_safe(update, message)
        return

    chunk_size = 10
    poem_chunks = [poems[i:i + chunk_size] for i in range(0, len(poems), chunk_size)]
    total_pages = len(poem_chunks)

    if page < 1 or page > total_pages:
        page = 1

    current_chunk = page - 1
    buttons = []
    current_poems = poem_chunks[current_chunk]

    # Split poems into two columns (5 each)
    mid_point = len(current_poems) // 2 + len(current_poems) % 2  # Handle odd number of poems
    left_column = current_poems[:mid_point]
    right_column = current_poems[mid_point:]

    # Create rows with two buttons each
    for left, right in zip_longest(left_column, right_column):
        row = []
        if left:
            row.append(InlineKeyboardButton(
                f"Бахши {left['poem_id']}", 
                callback_data=f"poem_{left['poem_id']}_{daftar_name}"
            ))
        if right:
            row.append(InlineKeyboardButton(
                f"Бахши {right['poem_id']}", 
                callback_data=f"poem_{right['poem_id']}_{daftar_name}"
            ))
        buttons.append(row)

    nav_buttons = []
    if current_chunk > 0:
        nav_buttons.append(InlineKeyboardButton(
            "⬅️ Қаблӣ", 
            callback_data=f"daftar_{daftar_name}_{page-1}"
        ))
    if current_chunk < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(
            "Баъдӣ ➡️", 
            callback_data=f"daftar_{daftar_name}_{page+1}"
        ))

    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton(
        "↩️ Ба дафтарҳо", 
        callback_data="back_to_daftars"
    )])

    buttons.append([InlineKeyboardButton(
        "🏠 Ба аввал", 
        callback_data="back_to_start"
    )])

    message_text = (
        f"📖 <b>{daftar_name}</b>\n"
        f"📄 Саҳифа {page} аз {total_pages}\n"
        f"Ҷамъи {total} бахш"
    )

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            text=message_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await send_message_safe(
            update,
            message_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(buttons)
        )

async def send_poem(update_or_query, poem_id, volume_number=None, show_full=False, part=0, search_term=""):
    try:
        poem = db.get_poem_by_id(poem_id, volume_number)
        if not poem:
            await send_message_safe(
                update_or_query, 
                "😔 Мутаассифона, шеър бо ин калима ёфт нашуд. Биёед дубора бо дигар тарз кӯшиш кунем! 🔎",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")
                ]])
            )
            return

        intro = (
            "━━━━━ 📚 <b>Маълумот</b> 📚 ━━━━━\n\n"
            f"📖 <b>Китоб:</b> {poem['book_title']}\n"
            f"📜 <b>Ҷилд:</b> {poem['volume_number']}\n"
            f"📑 <b>Бахш:</b> {poem['poem_id']}\n"
            f"🔹 <b>Мавзӯъ:</b> {poem['section_title']}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        poem_text = poem['poem_text']
        if search_term:
            poem_text = highlight_text(poem_text, search_term)

        # Split into parts of maximum 3000 characters to leave room for intro and formatting
        text_parts = split_long_message(poem_text, max_length=3000)
        total_parts = len(text_parts)

        if show_full or total_parts == 1:
            current_part = text_parts[part]
            message_text = f"{intro}<pre>{current_part}</pre>"

            if total_parts > 1:
                message_text += f"\n\n📄 Қисми {part + 1} аз {total_parts}"

            keyboard = []
            nav_buttons = []

            if total_parts > 1:
                if part > 0:
                    nav_buttons.append(InlineKeyboardButton(
                        "⬅️ Қисми қаблӣ", 
                        callback_data=f"poem_{poem_id}_{poem['volume_number']}_{part-1}"
                    ))
                if part < total_parts - 1:
                    nav_buttons.append(InlineKeyboardButton(
                        "Қисми баъдӣ ➡️", 
                        callback_data=f"poem_{poem_id}_{poem['volume_number']}_{part+1}"
                    ))
                if nav_buttons:
                    keyboard.append(nav_buttons)

            # Add back button based on context
            back_button = []
            if hasattr(update_or_query, 'data') and 'full_poem_' in update_or_query.data:
                back_button.append(InlineKeyboardButton(
                    "↩️ Ба мисраи рӯз",
                    callback_data=f"back_to_daily_{poem_id}"
                ))
            else:
                daftar_name = poem['volume_number']
                back_button.append(InlineKeyboardButton(
                    f"↩️ Ба {daftar_name}",
                    callback_data=f"daftar_{daftar_name}"
                ))
            keyboard.append(back_button)
            keyboard.append([InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")])

            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

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
                # If HTML formatting fails, try sending without formatting
                try:
                    plain_text = f"{poem['book_title']}\n{poem['volume_number']} - Бахши {poem['poem_id']}\n{poem['section_title']}\n\n{current_part}"
                    if total_parts > 1:
                        plain_text += f"\n\nҚисми {part + 1} аз {total_parts}"
                    await send_message_safe(
                        update_or_query, 
                        plain_text,
                        reply_markup=reply_markup
                    )
                except Exception as e2:
                    logger.error(f"Error sending plain text: {e2}")
                    await send_message_safe(
                        update_or_query, 
                        "⚠️ Хатогӣ дар фиристодани матн."
                    )

        else:
            # Show preview with "read full" button if not showing full
            preview_length = min(len(text_parts[0]), 1000)  # Limit preview to 1000 chars
            preview_text = text_parts[0][:preview_length] + "\n\n... (давомаш ҳаст)"
            message_text = f"{intro}<pre>{preview_text}</pre>"

            keyboard = [
                [InlineKeyboardButton("📖 Шеъри пурра", callback_data=f"poem_{poem_id}_{poem['volume_number']}_0")],
                [InlineKeyboardButton(f"↩️ Ба {poem['volume_number']}", callback_data=f"daftar_{poem['volume_number']}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await send_message_safe(
                update_or_query,
                message_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )

    except Exception as e:
        logger.error(f"Unexpected error in send_poem: {e}")
        await send_message_safe(update_or_query, "⚠️ Хатогӣ ҳангоми фиристодани шеър.")

async def divan_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get first page of ghazals
        ghazals, total_count = db.get_divan_ghazals(page=1)
        per_page = 10
        total_pages = (total_count + per_page - 1) // per_page

        buttons = []
        # Create buttons in pairs
        for i in range(0, len(ghazals), 2):
            row = []
            # Add first button
            row.append(InlineKeyboardButton(
                f"Ғазали {ghazals[i]['poem_id']}", 
                callback_data=f"divan_poem_{ghazals[i]['poem_id']}"
            ))
            # Add second button if exists
            if i + 1 < len(ghazals):
                row.append(InlineKeyboardButton(
                    f"Ғазали {ghazals[i+1]['poem_id']}", 
                    callback_data=f"divan_poem_{ghazals[i+1]['poem_id']}"
                ))
            buttons.append(row)

        # Add navigation buttons if needed
        nav_buttons = []
        if total_pages > 1:
            nav_buttons.append(InlineKeyboardButton("Баъдӣ ➡️", callback_data="divan_page_2"))
        if nav_buttons:
            buttons.append(nav_buttons)

        buttons.append([InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")])

        message_text = (
            "📖 <b>Девони Шамс</b>\n\n"
            f"Ҷамъи ғазалҳо: {total_count}\n"
            f"Саҳифа: 1 аз {total_pages}\n\n"
            "Лутфан ғазалро интихоб кунед:"
        )

        if isinstance(update, Update) and update.callback_query:
            await update.callback_query.edit_message_text(
                text=message_text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode='HTML'
            )
        else:
            await send_message_safe(
                update,
                message_text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode='HTML'
            )
    except Exception as e:
        logger.error(f"Error in divan_info: {e}")
        await send_message_safe(update, "⚠️ Хатогӣ дар гирифтани маълумот.")

async def daily_verse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /daily command to show today's verse"""
    try:
        verse = db.get_daily_verse()

        if not verse:
            keyboard = [[InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]]
            message_text = "⚠️ Мисраи рӯз ёфт нашуд. Лутфан баъдтар боз кӯшиш кунед."
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=message_text,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await update.message.reply_text(
                    text=message_text,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            return

        today = date.today().strftime("%d.%m.%Y")
        message_text = (
            "━━━━━ ⭐️ <b>Мисраи рӯз</b> ⭐️ ━━━━━\n\n"
            f"📅 <b>Сана:</b> {today}\n"
            f"📖 <b>Китоб:</b> {verse['book_title']}\n"
            f"📜 <b>Ҷилд ва бахш:</b> {verse['volume_number']} - {verse['poem_id']}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<i>{verse['verse_text']}</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━"
        )

        # Store the verse information in context for later use
        context.user_data['current_daily_verse'] = {
            'unique_id': verse['unique_id'],
            'poem_id': verse['poem_id'],
            'volume_number': verse['volume_number'],
            'book_title': verse['book_title'],
            'verse_text': verse['verse_text']
        }

        keyboard = [
            [InlineKeyboardButton("📖 Шеъри пурра", callback_data=f"full_poem_{verse['unique_id']}")],
            [InlineKeyboardButton("⭐️ Мисраи дигар", callback_data="daily_verse")],
            [InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]
        ]

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=message_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=message_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    except Exception as e:
        logger.error(f"Error in daily verse: {e}")
        error_message = "⚠️ Хатогӣ дар гирифтани мисраи рӯз. Лутфан баъдтар боз кӯшиш кунед."
        keyboard = [[InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]]
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=error_message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=error_message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

async def search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 Ҷустуҷӯ аз рӯи калима", callback_data="search_by_word")],
        [InlineKeyboardButton("📝 Ҷустуҷӯ аз рӯи мисраъ", callback_data="search_by_verse")],
        [InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]
    ]

    await send_message_safe(
        update,
        "🔍 <b>Ҷустуҷӯ дар ашъори Мавлоно</b>\n\n"
        "Лутфан навъи ҷустуҷӯро интихоб кунед:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search_term = ' '.join(context.args).strip()
    if not search_term:
        keyboard = [
            [InlineKeyboardButton("🔍 Аз нав кӯшиш кунед", callback_data="search_menu")],
            [InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]
        ]
        await send_message_safe(
            update, 
            "⚠️ Лутфан калима ё мисраро барои ҷустуҷӯ ворид кунед.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    poems = db.search_poems(search_term)
    if not poems:
        keyboard = [
            [InlineKeyboardButton("🔍 Аз нав ҷустуҷӯ", callback_data="search_menu")],
            [InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]
        ]
        await send_message_safe(
            update,
            f"⚠️ Ҳеҷ шеъре барои <b>'{search_term}'</b> ёфт нашуд.\n\nЛутфан калимаи дигарро истифода баред.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        return

    for poem in poems:
        highlighted = highlight_text(poem['poem_text'], search_term)
        text_parts = split_long_message(highlighted)

        intro = (
            f"📖 <b>{poem['book_title']}</b>\n"
            f"📜 <b>{poem['volume_number']} - Бахши {poem['poem_id']}</b>\n"
            f"🔹 {poem['section_title']}\n\n"
        )

        keyboard = [[
            InlineKeyboardButton(f"↩️ Ба {poem['volume_number']}", callback_data=f"back_to_daftar_{poem['volume_number']}")
        ]]

        for i, part in enumerate(text_parts):
            message_text = f"{intro}{part}"
            if i == len(text_parts) - 1:
                await send_message_safe(
                    update, 
                    message_text, 
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await send_message_safe(update, message_text, parse_mode='HTML')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.startswith("/"):
        return  # Let command handlers handle commands

    # Check if we're waiting for a poem
    if context.user_data.get('waiting_for_poem', False):
        try:
            success = db.add_mixed_poem(text)
            if success:
                await update.message.reply_text(
                    f"✅ Шеър бомуваффақият илова шуд:\n\n<pre>{text}</pre>", 
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text("⛔ Ин шеър аллакай мавҷуд аст.")
        except Exception as e:
            logger.error(f"Error adding mixed poem: {e}")
            await update.message.reply_text("❌ Хатогӣ дар иловаи шеър.")
        finally:
            context.user_data['waiting_for_poem'] = False
        return

    # If not waiting for poem, treat as search
    context.args = [text]  # Set the search term
    await search(update, context)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('waiting_for_poem'):
        context.user_data['waiting_for_poem'] = False
        await update.message.reply_text("❌ Иловаи шеър бекор карда шуд.")
    else:
        await update.message.reply_text("❓ Ягон амали фаъол нест.")


async def handle_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("📚 Маснавии Маънавӣ", callback_data="masnavi_info"),
            InlineKeyboardButton("📖 Девони Шамс", callback_data="divan_info")
        ],
        [
            InlineKeyboardButton("ℹ️ Дар бораи Балхӣ", callback_data="balkhi_info"),
            InlineKeyboardButton("⭐️ Мисраи рӯз", callback_data="daily_verse")
        ],
        [InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]
    ]

    await send_message_safe(
        update,
        "Лутфан аз тугмаҳои зерин истифода баред ё бо фармони /search ҷустуҷӯ кунед:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    try:
        await query.answer("⏳ Интизор шавед...")

        if data == "masnavi_info":
            await masnavi_info(update, context)
        elif data == "divan_info":
            await divan_info(update, context)
        elif data == "random_poem":
            await random_command(update, context)
        elif data == "daily_verse":
            await daily_verse(update, context)
        elif data.startswith("divan_page_"):
            try:
                page = int(data.split("_")[2])
                ghazals, total_count = db.get_divan_ghazals(page=page)
                per_page = 10
                total_pages = (total_count + per_page - 1) // per_page

                buttons = []
                # Create buttons in pairs
                for i in range(0, len(ghazals), 2):
                    row = []
                    # Add first button
                    row.append(InlineKeyboardButton(
                        f"Ғазали {ghazals[i]['poem_id']}", 
                        callback_data=f"divan_poem_{ghazals[i]['poem_id']}"
                    ))
                    # Add second button if exists
                    if i + 1 < len(ghazals):
                        row.append(InlineKeyboardButton(
                            f"Ғазали {ghazals[i+1]['poem_id']}", 
                            callback_data=f"divan_poem_{ghazals[i+1]['poem_id']}"
                        ))
                    buttons.append(row)

                # Add navigation buttons if needed
                nav_buttons = []
                if page > 1:
                    nav_buttons.append(InlineKeyboardButton("⬅️ Қаблӣ", callback_data=f"divan_page_{page-1}"))
                if page < total_pages:
                    nav_buttons.append(InlineKeyboardButton("Баъдӣ ➡️", callback_data=f"divan_page_{page+1}"))
                if nav_buttons:
                    buttons.append(nav_buttons)

                buttons.append([InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")])

                message_text = (
                    "📖 <b>Девони Шамс</b>\n\n"
                    f"Ҷамъи ғазалҳо: {total_count}\n"
                    f"Саҳифа: {page} аз {total_pages}\n\n"
                    "Лутфан ғазалро интихоб кунед:"
                )

                await query.edit_message_text(
                    text=message_text,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Error handling divan page callback: {e}")
                await query.answer("⚠️ Хатогӣ дар гирифтани саҳифа", show_alert=True)
        elif data.startswith("divan_poem_"):
            try:
                ghazal_id = int(data.split("_")[2])
                ghazal = db.get_divan_ghazal_by_id(ghazal_id)
                if ghazal:
                    message_text = (
                        "━━━━━ 📚 <b>Маълумот</b> 📚 ━━━━━\n\n"
                        f"📖 <b>Китоб:</b> Девони Шамс\n"
                        f"📑 <b>Ғазал:</b> {ghazal['section_title']}\n\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"<pre>{ghazal['poem_text']}</pre>"
                    )
                    keyboard = [
                        [InlineKeyboardButton("⬅️ Ба Ғазалҳо ", callback_data="divan_info")],
                        [InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]
                    ]
                    await query.edit_message_text(
                        text=message_text,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await query.answer("⚠️ Ғазал ёфт нашуд", show_alert=True)
            except Exception as e:
                logger.error(f"Error handling divan poem callback: {e}")
                await query.answer("⚠️ Хатогӣ дар гирифтани ғазал", show_alert=True)
        elif data.startswith("poem_"):
            try:
                parts = data.split("_")
                poem_id = int(parts[1])
                volume_number = parts[2] if len(parts) > 2 else None
                part = int(parts[3]) if len(parts) > 3 else 0
                await send_poem(query, poem_id, volume_number=volume_number, show_full=True, part=part)
            except Exception as e:
                logger.error(f"Error handling poem callback: {e}")
                await query.answer("⚠️ Хатогӣ дар гирифтани шеър", show_alert=True)
        elif data.startswith("daftar_"):
            parts = data.split("_")
            daftar_name = parts[1]
            if len(parts) > 2:
                page = int(parts[2])
                await show_poems_page(update, context, daftar_name, page)
            else:
                await show_poems_page(update, context, daftar_name)
        elif data == "back_to_daftars":
            await masnavi_info(update, context)
        elif data == "back_to_start":
            await start(update, context)
        elif data == "unavailable_daftar":
            await query.answer("Ин дафтар айни ҳол дастрас нест", show_alert=True)
        elif data.startswith("back_to_daily_"):
            poem_id = int(data.split("_")[2])
            verse = db.execute_query(
                "SELECT p.*, hv.verse_text FROM highlighted_verses hv "
                "JOIN poems p ON p.unique_id = hv.poem_unique_id "
                "WHERE p.poem_id = %s",
                (poem_id,),
                fetch=True
            )
            if verse:
                message_text = (
                    f"🌟 <b>Мисраи рӯз</b> 🌟\n\n"
                    f"📖 <b>{verse[0]['book_title']}</b>\n"
                    f"📜 <b>{verse[0]['volume_number']} - Бахши {verse[0]['poem_id']}</b>\n\n"
                    f"<i>{verse[0]['verse_text']}</i>"
                )
                keyboard = [[
                    InlineKeyboardButton("📖 Шеъри пурра", callback_data=f"poem_{verse[0]['unique_id']}_0")
                ]]
                await query.edit_message_text(
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

    except telegram.error.TimedOut:
        logger.error("Telegram API timeout in button_callback")
        try:
            await query.answer("⚠️ Хадамоти Telegram вақт ба охир расид. Лутфан аз нав кӯшиш кунед.", show_alert=True)
        except:
            pass
    except telegram.error.NetworkError:
        logger.error("Network error in button_callback")
        try:
            await query.answer("⚠️ Хатои шабака. Лутфан пайвасти интернетро санҷед ва аз нав кӯшиш кунед.", show_alert=True)
        except:
            pass
    except Exception as e:
        logger.error(f"Error in button_callback: {e}")
        try:
            await query.answer("⚠️ Хатогӣ дар коркарди фармонат рух дод. Лутфан аз нав кӯшиш кунед.", show_alert=True)
        except:
            pass

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📜 <b>Роҳнамо</b> — <i>Истифодабарӣ ва идоракунӣ</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Барои истифодабарандагон:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏠 /start — Бозгашт ба менюи асосӣ\n"
        "🔍 /search &lt;калима ё ибора&gt; — Ҷустуҷӯ дар тамоми ашъори Мавлоно\n"
        "⭐️ /daily — Мисраи рӯза\n"
        "🎲 /random — Шеъри тасодуфӣ\n"
        "📖 /info — Маълумот дар бораи Мавлоно Ҷалолуддини Балхӣ\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛡 <b>Барои админҳо:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📍 /highlight &lt;unique_id&gt; &lt;матни мисра&gt; — Илова кардани мисраи махсус\n"
        "🗑 /delete_highlight &lt;highlight_id&gt; — Ҳазфи мисраи махсус\n"
        "📍 /addpoem — Илова кардани шеър ба рӯйхати шеърҳои тасодуфӣ\n"
        "📊 /get_data &lt;номи ҷадвал&gt; — Гирифтани маълумот аз ҷадвал\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ <i>Эзоҳ:</i>\n"
        "- Барои ҷустуҷӯ фармони /search -ро нависед.\n"
        "- Мисраҳои рӯз ҳар рӯза нав мешаванд.\n"
        "- Шеърҳои тасодуфӣ ҳар рӯз такрор намешаванд.\n"
        "- Админҳо функсияҳои махсус доранд.\n"
        "- Ҷадвалҳои дастрас: mixed_poems, poem_history, divan_poems, poems, highlighted_verses\n\n"
        "🤗 Ҳар савол ё пешниҳоде ки барои боз ҳам беҳтар намудани бот доред бо мо @zabirovms дар тамос шавед!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Ба аввал", callback_data="back_to_start")]
    ])

    await send_message_safe(
        update,
        help_text,
        parse_mode='HTML',
        reply_markup=keyboard
    )


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
        verse_text = ' '.join(context.args[1:])
        verse_text = verse_text.replace('||', '\n')  # convert line markers to actual line breaks


        if db.is_highlight_exists(poem_unique_id, verse_text):
            await update.message.reply_text("⚠️ Ин мисра аллакай дар <i>highlighted_verses</i> мавҷуд аст.", parse_mode='HTML')
            return

        db.add_highlighted_verse(poem_unique_id, verse_text)
        await update.message.reply_text(f"✅ Мисра ба <i>highlighted_verses</i> илова шуд:\n\n<pre>{verse_text}</pre>", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error adding highlighted verse: {e}")
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
        logger.error(f"Error deleting highlighted verse: {e}")
        await update.message.reply_text("❌ Хатогӣ дар ҳазфи мисра.")

async def add_mixed_poem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔️ Шумо иҷозати илова кардани шеърро надоред.")
        return

    context.user_data['waiting_for_poem'] = True
    await update.message.reply_text(
        "Лутфан матни шеърро ворид кунед.\n\n"
        "Барои бекор кардан /cancel -ро пахш кунед."
    )

async def post_daily_poem(context: ContextTypes.DEFAULT_TYPE):
    try:
        poem = db.get_random_mixed_poem()
        if poem:
            # Decorate the poem
            decorated_poem = (
                "📜 Шеъри Рӯз 📜\n\n"
                f"<blockquote>{poem['poem_text']}</blockquote>"
            )
            # Send to Telegram channel
            await context.bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,  # Replace with your channel ID
                text=decorated_poem,
                parse_mode='HTML'
            )
            logger.info("Daily poem posted successfully.")
        else:
            logger.warning("No poems found in mixed_poems table.")
    except Exception as e:
        logger.error(f"Error posting daily poem: {e}")

async def get_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export table data to Excel file"""
    user_id = update.effective_user.id
    
    # Check if user is admin
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔️ Шумо иҷозати иҷрои ин фармонро надоред.")
        return

    # Check if table name is provided
    if not context.args:
        await update.message.reply_text(
            "⚠️ Лутфан номи ҷадвалро муайян кунед.\n"
            "Масалан: /get_data mixed_poems"
        )
        return

    table_name = context.args[0].lower()
    
    # List of allowed tables for security
    allowed_tables = [
        'mixed_poems',
        'poem_history',
        'divan_poems',
        'poems',
        'highlighted_verses'
    ]
    
    if table_name not in allowed_tables:
        await update.message.reply_text(
            "❌ Номи ҷадвал нодуруст аст.\n"
            "Ҷадвалҳои иҷозатдодашуда:\n" + 
            "\n".join(f"- {table}" for table in allowed_tables)
        )
        return

    try:
        # Send "processing" message
        processing_msg = await update.message.reply_text("⏳ Дар ҳоли тайёр кардани файл...")

        # Get data from the table
        data = db.execute_query(
            f"SELECT * FROM {table_name}",
            fetch=True
        )

        if not data:
            await processing_msg.edit_text("❌ Дар ин ҷадвал маълумот нест.")
            return

        # Convert to DataFrame
        df = pd.DataFrame(data)
        
        # Create Excel file in memory
        excel_file = BytesIO()
        df.to_excel(excel_file, index=False, engine='openpyxl')
        excel_file.seek(0)

        # Send the file
        await update.message.reply_document(
            document=excel_file,
            filename=f"{table_name}_{date.today().strftime('%Y%m%d')}.xlsx",
            caption=f"📊 Маълумот аз ҷадвали '{table_name}'\n📅 {date.today().strftime('%d.%m.%Y')}"
        )

        # Delete processing message
        await processing_msg.delete()

    except Exception as e:
        logger.error(f"Error exporting data from {table_name}: {e}")
        await update.message.reply_text(
            "❌ Хатогӣ дар гирифтани маълумот.\n"
            "Лутфан баъдтар боз кӯшиш кунед."
        )

async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /random command to show a random poem"""
    user_id = update.effective_user.id
    
    # Check subscription only for random poems
    if user_id not in ADMIN_USER_IDS:
        try:
            channel_id = TELEGRAM_CHANNEL_ID
            if not channel_id.startswith('@') and not channel_id.startswith('-100'):
                channel_id = f"@{channel_id.lstrip('@')}"
                
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                keyboard = [[
                    InlineKeyboardButton("📢 Обуна шудан", url="https://t.me/balkhiverses"),
                    InlineKeyboardButton("🔄 Тафтиш", callback_data="check_subscription")
                ]]
                message_text = "❗️ Барои истифодаи шеърҳои тасодуфӣ, лутфан ба канали мо обуна шавед:"
                
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text=message_text,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await update.message.reply_text(
                        text=message_text,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                return
        except Exception as e:
            logger.error(f"Error checking subscription: {e}")
            # In case of error, allow access to prevent blocking legitimate users
            pass

    try:
        # Get a random poem that hasn't been shown today
        today = date.today().strftime("%Y-%m-%d")
        poem = db.execute_query(
            """
            SELECT mp.* 
            FROM mixed_poems mp
            LEFT JOIN poem_history ph ON mp.id = ph.poem_id 
                AND ph.date_shown = %s
            WHERE ph.id IS NULL
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (today,),
            fetch=True
        )

        if not poem:
            # If all poems have been shown today, reset the history
            db.execute_query(
                "DELETE FROM poem_history WHERE date_shown = %s",
                (today,)
            )
            # Try again
            poem = db.execute_query(
                "SELECT * FROM mixed_poems ORDER BY RANDOM() LIMIT 1",
                fetch=True
            )

        if poem:
            # Record that this poem was shown today
            db.execute_query(
                "INSERT INTO poem_history (poem_id, date_shown) VALUES (%s, %s)",
                (poem[0]['id'], today)
            )

            message_text = (
                "🎲 <b>Шеъри тасодуфӣ</b> 🎲\n\n"
                f"<pre>{poem[0]['poem_text']}</pre>"
            )

            keyboard = [
                [InlineKeyboardButton("🎲 Шеъри дигар", callback_data="random_poem")],
                [InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]
            ]

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await update.message.reply_text(
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        else:
            error_message = "⚠️ Ҳеҷ шеъре ёфт нашуд. Лутфан баъдтар боз кӯшиш кунед."
            keyboard = [[InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]]
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=error_message,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await update.message.reply_text(
                    text=error_message,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
    except Exception as e:
        logger.error(f"Error in random command: {e}")
        error_message = "⚠️ Хатогӣ дар гирифтани шеъри тасодуфӣ. Лутфан баъдтар боз кӯшиш кунед."
        keyboard = [[InlineKeyboardButton("🏠 Ба саҳифаи аввал", callback_data="back_to_start")]]
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=error_message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=error_message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

def main():
    # Check if required environment variables are set
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("❌ Required environment variables not set!")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("daily", daily_verse))
    application.add_handler(CommandHandler("verse", daily_verse))
    application.add_handler(CommandHandler("info", balkhi_info))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("random", random_command))
    application.add_handler(CommandHandler("highlight", highlight_verse))
    application.add_handler(CommandHandler("delete_highlight", delete_highlight))
    application.add_handler(CommandHandler("addpoem", add_mixed_poem_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("get_data", get_data))

    # Message handlers
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_text))

    # Other content types handler
    application.add_handler(MessageHandler(
        filters.ALL & ~filters.TEXT & ~filters.COMMAND, 
        handle_invalid_input))

    # Callback handlers
    application.add_handler(CallbackQueryHandler(button_callback))

    try:
        # Schedule daily poem posting (at 9:00 AM UTC)
        if hasattr(application, 'job_queue'):
            job_queue = application.job_queue
            job_queue.run_daily(
                post_daily_poem,
                time=time(9, 0),  # 9:00 AM UTC
                days=(0, 1, 2, 3, 4, 5, 6)
            )
            logger.info("Daily poem posting scheduled successfully")
        else:
            logger.warning("Job queue is not available. Daily posting will not work.")
    except Exception as e:
        logger.error(f"Failed to schedule daily poem posting: {e}")

    # Start the bot
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
