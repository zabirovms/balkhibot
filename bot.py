import os
import logging
import psycopg2
import time
import re
from typing import Optional, Dict, List, Any, Union
from psycopg2.extras import DictCursor
from telegram import (
    ReplyKeyboardMarkup,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get environment variables
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_USER_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []

# Constants
MAX_MESSAGE_LENGTH = 4000
DAFTAR_NAMES = [
    {'volume_number': 'Дафтари 1', 'volume_num': 1},
    {'volume_number': 'Дафтари 2', 'volume_num': 2},
    {'volume_number': 'Дафтари 3', 'volume_num': 3},
    {'volume_number': 'Дафтари 4', 'volume_num': 4},
    {'volume_number': 'Дафтари 5', 'volume_num': 5},
    {'volume_number': 'Дафтари 6', 'volume_num': 6}
]

# Emoji constants for better visual organization
EMOJI = {
    'home': '🏠',
    'back': '↩️',
    'search': '🔍',
    'book': '📖',
    'poem': '📜',
    'info': 'ℹ️',
    'daily': '🌟',
    'next': '➡️',
    'prev': '⬅️',
    'loading': '⏳',
    'error': '❌',
    'success': '✅',
    'admin': '⚙️',
    'divan': '🌹',
    'masnavi': '🕌'
}

class DatabaseManager:
    def __init__(self, max_retries: int = 3, retry_delay: int = 2):
        self.conn = None
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.connect_with_retry()
        self._ensure_database_integrity()

    def _ensure_database_integrity(self) -> None:
        """Ensure all required database structure exists"""
        try:
            # Check and add unique_id to poems if not exists
            if not self.execute_query("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'poems' AND column_name = 'unique_id'
                """, fetch=True):
                self.execute_query("ALTER TABLE poems ADD COLUMN unique_id SERIAL PRIMARY KEY")
                logger.info("Added unique_id to poems table")

            # Recreate highlighted_verses with proper foreign key if not exists
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
                logger.info("Created new highlighted_verses table")

                # Migrate existing data if needed
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
                    logger.info("Migrated data to highlighted_verses")

            # Add indexes for performance
            self.execute_query("""
                CREATE INDEX IF NOT EXISTS idx_poems_unique_id ON poems(unique_id)
                """)
            self.execute_query("""
                CREATE INDEX IF NOT EXISTS idx_hv_poem_unique_id ON highlighted_verses(poem_unique_id)
                """)

        except Exception as e:
            logger.error(f"Error ensuring database integrity: {e}")
            raise

    def connect_with_retry(self) -> None:
        for attempt in range(self.max_retries):
            try:
                self.conn = psycopg2.connect(DATABASE_URL, sslmode='require')
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

    def execute_query(self, query: str, params: Optional[tuple] = None, fetch: bool = False) -> Any:
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

    def get_all_daftars(self) -> List[Dict[str, Any]]:
        daftars = DAFTAR_NAMES.copy()

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

    def get_poems_by_daftar(self, daftar_name: str) -> List[Dict[str, Any]]:
        query = """
        SELECT poem_id, section_title
        FROM poems
        WHERE volume_number = %s
        ORDER BY poem_id
        """
        return self.execute_query(query, (daftar_name,), fetch=True) or []

    def search_poems(self, search_term: str) -> List[Dict[str, Any]]:
        query = """
        SELECT poem_id, book_title, volume_number, section_title, poem_text
        FROM poems
        WHERE poem_tsv @@ plainto_tsquery('simple', %s)
        ORDER BY ts_rank(poem_tsv, plainto_tsquery('simple', %s)) DESC
        LIMIT 50
        """
        return self.execute_query(query, (search_term, search_term), fetch=True) or []

    def get_poem_by_id(self, poem_id: int) -> Optional[Dict[str, Any]]:
        query = "SELECT * FROM poems WHERE poem_id = %s"
        result = self.execute_query(query, (poem_id,), fetch=True)
        return result[0] if result else None

    def get_daily_verse(self) -> Optional[Dict[str, Any]]:
        query = """
        SELECT p.*, hv.verse_text
        FROM highlighted_verses hv
        JOIN poems p ON p.unique_id = hv.poem_unique_id
        ORDER BY RANDOM()
        LIMIT 1
        """
        result = self.execute_query(query, fetch=True)
        return result[0] if result else None

    def add_highlighted_verse(self, poem_unique_id: int, verse_text: str) -> None:
        query = """
        INSERT INTO highlighted_verses (poem_unique_id, verse_text)
        VALUES (%s, %s)
        """
        self.execute_query(query, (poem_unique_id, verse_text))

    def is_highlight_exists(self, poem_unique_id: int, verse_text: str) -> bool:
        query = """
        SELECT 1 FROM highlighted_verses
        WHERE poem_unique_id = %s AND verse_text = %s
        LIMIT 1
        """
        return bool(self.execute_query(query, (poem_unique_id, verse_text), fetch=True))

    def delete_highlighted_verse(self, highlight_id: int) -> None:
        query = "DELETE FROM highlighted_verses WHERE id = %s"
        self.execute_query(query, (highlight_id,))

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed.")

# Initialize database connection
db = DatabaseManager()

# Utility functions
def highlight_text(text: str, search_term: str) -> str:
    if not search_term:
        return text
    try:
        safe_term = re.escape(search_term)
        return re.sub(f"({safe_term})", r"<b>\1</b>", text, flags=re.IGNORECASE)
    except Exception as e:
        logger.warning(f"Highlighting failed: {e}")
        return text

def split_long_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
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

async def send_message_safe(
    update_or_query: Union[Update, CallbackQuery],
    text: str,
    parse_mode: str = 'HTML',
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs
) -> None:
    try:
        if isinstance(update_or_query, Update) and update_or_query.message:
            await update_or_query.message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs
            )
        elif isinstance(update_or_query, CallbackQuery):
            await update_or_query.edit_message_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs
            )
        elif hasattr(update_or_query, 'reply_text'):
            await update_or_query.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **kwargs
            )
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        if len(text) > MAX_MESSAGE_LENGTH:
            parts = split_long_message(text)
            for part in parts:
                await send_message_safe(
                    update_or_query,
                    part,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    **kwargs
                )

async def show_loading(update_or_query: Union[Update, CallbackQuery], text: str = f"{EMOJI['loading']} Интизор шавед...") -> None:
    """Show a loading message while processing data"""
    await send_message_safe(
        update_or_query,
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("...", callback_data="loading")]])
    )

# ================== COMMAND HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [f"{EMOJI['masnavi']} Маснавии Маънавӣ"],
        [f"{EMOJI['divan']} Девони Шамс"],
        [f"{EMOJI['search']} Ҷустуҷӯ", f"{EMOJI['info']} Маълумот"],
        [f"{EMOJI['daily']} Мисраи рӯз"]
    ]
    await send_message_safe(
        update,
        f"""🌟 <b>Асарҳои Мавлоно Ҷалолуддини Балхӣ</b> 🌟

Аз ин рӯйхат як интихоб кунед:""",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False),
        parse_mode='HTML'
    )

async def balkhi_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    info_text = f"""
📚 <b>Маълумот дар бораи Мавлоно Ҷалолуддини Балхӣ</b>

Мавлоно Ҷалолуддини Муҳаммади Балхӣ (Румӣ) аз бузургтарин шоирони адабиёти форс-тоҷик аст. Осори ӯ аз ҷумлаи гаронбаҳотарин мероси адабии тоҷикон мебошад.
"""

    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['book']} Маснавии Маънавӣ", callback_data="masnavi_info")],
        [InlineKeyboardButton(f"{EMOJI['book']} Девони Шамс", callback_data="divan_info")],
        [InlineKeyboardButton(f"{EMOJI['back']} Бозгашт", callback_data="back_to_start")]
    ]

    await send_message_safe(
        update,
        info_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def masnavi_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_loading(update, f"{EMOJI['loading']} Дафтарҳо бор карда мешаванд...")
    
    try:
        daftars = db.get_all_daftars()
        buttons = []
        
        for daftar in daftars:
            if daftar['available']:
                buttons.append([InlineKeyboardButton(
                    f"{EMOJI['book']} {daftar['volume_number']}",
                    callback_data=f"daftar_{daftar['volume_number']}"
                )])
            else:
                buttons.append([InlineKeyboardButton(
                    f"{EMOJI['error']} {daftar['volume_number']} (дастрас нест)",
                    callback_data="unavailable_daftar"
                )])

        buttons.append([InlineKeyboardButton(f"{EMOJI['back']} Бозгашт", callback_data="back_to_start")])

        await send_message_safe(
            update,
            f"""📖 <b>Маснавии Маънавӣ</b>

Интихоб кардани дафтар:""",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Error in masnavi_info: {e}")
        await send_message_safe(
            update,
            f"""{EMOJI['error']} <b>Хатогӣ</b>

Дафтарҳо бор карда нашуданд. Лутфан баъдтар аз нав кӯшиш кунед.""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['back']} Аз нав кӯшиш кунед", callback_data="masnavi_info")],
                [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
            ]),
            parse_mode='HTML'
        )

async def show_poems_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    daftar_name: str,
    page: int = 1
) -> None:
    await show_loading(update, f"{EMOJI['loading']} Боргирии шеърҳо...")
    
    try:
        poems = db.get_poems_by_daftar(daftar_name)
        total = len(poems)

        if not poems:
            await send_message_safe(
                update,
                f"""{EMOJI['error']} <b>Хатогӣ</b>

Шеърҳо дар ин дафтар ёфт нашуд.""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['back']} Ба дафтарҳо", callback_data="back_to_daftars")],
                    [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
                ]),
                parse_mode='HTML'
            )
            return

        chunk_size = 10
        poem_chunks = [poems[i:i + chunk_size] for i in range(0, len(poems), chunk_size)]
        total_pages = len(poem_chunks)

        if page < 1 or page > total_pages:
            page = 1

        current_chunk = page - 1
        buttons = []
        for poem in poem_chunks[current_chunk]:
            buttons.append([InlineKeyboardButton(
                f"{EMOJI['poem']} Бахши {poem['poem_id']}: {poem['section_title'][:30]}...",
                callback_data=f"poem_{poem['poem_id']}"
            )])

        nav_buttons = []
        if current_chunk > 0:
            nav_buttons.append(InlineKeyboardButton(
                f"{EMOJI['prev']} Қаблӣ",
                callback_data=f"daftar_{daftar_name}_{page-1}"
            ))
        if current_chunk < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(
                f"Баъдӣ {EMOJI['next']}",
                callback_data=f"daftar_{daftar_name}_{page+1}"
            ))

        if nav_buttons:
            buttons.append(nav_buttons)

        buttons.append([InlineKeyboardButton(
            f"{EMOJI['back']} Ба дафтарҳо",
            callback_data="back_to_daftars"
        )])

        message_text = (
            f"📖 <b>{daftar_name}</b>\n"
            f"📄 Саҳифа {page} аз {total_pages}\n"
            f"Ҷамъи {total} бахш"
        )

        await send_message_safe(
            update,
            message_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Error in show_poems_page: {e}")
        await send_message_safe(
            update,
            f"""{EMOJI['error']} <b>Хатогӣ</b>

Шеърҳо бор карда нашуданд. Лутфан баъдтар аз нав кӯшиш кунед.""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['back']} Аз нав кӯшиш кунед", callback_data=f"daftar_{daftar_name}_{page}")],
                [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
            ]),
            parse_mode='HTML'
        )

async def send_poem(
    update_or_query: Union[Update, CallbackQuery],
    poem_id: int,
    show_full: bool = False,
    part: int = 0,
    search_term: str = "",
    current_daftar: Optional[str] = None
) -> None:
    await show_loading(update_or_query, f"{EMOJI['loading']} Шеър бор карда мешавад...")
    
    try:
        poem = db.get_poem_by_id(poem_id)
        if not poem:
            await send_message_safe(
                update_or_query,
                f"""{EMOJI['error']} <b>Хатогӣ</b>

Шеъри дархостшуда ёфт нашуд.""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
                ]),
                parse_mode='HTML'
            )
            return

        intro = (
            f"📖 <b>{poem['book_title']}</b>\n"
            f"📜 <b>{poem['volume_number']} - Бахши {poem['poem_id']}</b>\n"
            f"🔹 {poem['section_title']}\n\n"
        )

        poem_text = poem['poem_text']
        if search_term:
            poem_text = highlight_text(poem_text, search_term)

        text_parts = split_long_message(poem_text)

        if show_full or len(text_parts) == 1:
            current_part = text_parts[part]
            message_text = f"{intro}<pre>{current_part}</pre>"

            keyboard = []
            if len(text_parts) > 1:
                nav_buttons = []
                if part > 0:
                    nav_buttons.append(InlineKeyboardButton(
                        f"{EMOJI['prev']} Қисми қаблӣ",
                        callback_data=f"poem_{poem_id}_{part-1}"
                    ))
                if part < len(text_parts) - 1:
                    nav_buttons.append(InlineKeyboardButton(
                        f"Қисми баъдӣ {EMOJI['next']}",
                        callback_data=f"poem_{poem_id}_{part+1}"
                    ))
                if nav_buttons:
                    keyboard.append(nav_buttons)

            daftar_name = current_daftar if current_daftar else poem['volume_number']
            keyboard.append([InlineKeyboardButton(
                f"{EMOJI['back']} Ба {daftar_name}",
                callback_data=f"back_to_daftar_{daftar_name}"
            )])

            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

            await send_message_safe(
                update_or_query,
                message_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            preview_text = text_parts[0] + "\n\n... (шеър тӯлонӣ аст)"
            message_text = f"{intro}<pre>{preview_text}</pre>"

            keyboard = [[
                InlineKeyboardButton(f"{EMOJI['book']} Шеъри пурра", callback_data=f"full_{poem_id}_0")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await send_message_safe(
                update_or_query,
                message_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Error in send_poem: {e}")
        await send_message_safe(
            update_or_query,
            f"""{EMOJI['error']} <b>Хатогӣ</b>

Шеър бор карда нашуд. Лутфан баъдтар аз нав кӯшиш кунед.""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['back']} Аз нав кӯшиш кунед", callback_data=f"poem_{poem_id}")],
                [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
            ]),
            parse_mode='HTML'
        )

async def divan_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    info_text = f"""
🌹 <b>Девони Шамс</b>

Девони Шамс ё Девони Кабир маҷмӯаи ғазалиёт ва ашъори лирикии Мавлоно Ҷалолуддини Балхӣ мебошад. Ин ашъор ба забони форсӣ суруда шудаанд.
"""

    await send_message_safe(
        update,
        info_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{EMOJI['back']} Бозгашт", callback_data="back_to_info")],
            [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
        ]),
        parse_mode='HTML'
    )

async def daily_verse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_loading(update, f"{EMOJI['loading']} Ҷустуҷӯи мисраи рӯз...")
    
    try:
        verse = db.get_daily_verse()
        if not verse:
            await send_message_safe(
                update,
                f"""{EMOJI['error']} <b>Хатогӣ</b>

Мисраи рӯз ёфт нашуд.""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['back']} Аз нав кӯшиш кунед", callback_data="daily_verse")],
                    [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
                ]),
                parse_mode='HTML'
            )
            return

        message_text = (
            f"🌟 <b>Мисраи рӯз</b> 🌟\n\n"
            f"📖 <b>{verse['book_title']}</b>\n"
            f"📜 <b>{verse['volume_number']} - Бахши {verse['poem_id']}</b>\n\n"
            f"<i>{verse['verse_text']}</i>"
        )

        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI['book']} Шеъри пурра", 
                    callback_data=f"full_{verse['poem_id']}_0_{verse['volume_number']}")
            ],
            [
                InlineKeyboardButton(f"{EMOJI['daily']} Мисраи нав", callback_data="daily_verse"),
                InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")
            ]
        ]

        await send_message_safe(
            update,
            message_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error in daily_verse: {e}")
        await send_message_safe(
            update,
            f"""{EMOJI['error']} <b>Хатогӣ</b>

Мисраи рӯз ёфт нашуд. Лутфан баъдтар аз нав кӯшиш кунед.""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['back']} Аз нав кӯшиш кунед", callback_data="daily_verse")],
                [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
            ]),
            parse_mode='HTML'
        )

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    search_term = ' '.join(context.args).strip()
    if not search_term:
        await send_message_safe(
            update,
            f"""{EMOJI['error']} <b>Хатогӣ</b>

Лутфан калима ё ибораро барои ҷустуҷӯ ворид кунед.

Мисол: /search ишқ""",
            reply_markup=ReplyKeyboardMarkup([[f"{EMOJI['home']} Ба аввал"]], resize_keyboard=True),
            parse_mode='HTML'
        )
        return

    await show_loading(update, f"{EMOJI['loading']} Ҷустуҷӯи '{search_term}'...")
    
    try:
        poems = db.search_poems(search_term)
        if not poems:
            await send_message_safe(
                update,
                f"""{EMOJI['error']} <b>Хатогӣ</b>

Ҳеҷ шеъре барои '{search_term}' ёфт нашуд.""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['search']} Ҷустуҷӯи нав", callback_data="search_again")],
                    [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
                ]),
                parse_mode='HTML'
            )
            return

        for poem in poems:
            highlighted = highlight_text(poem['poem_text'], search_term)
            text_parts = split_long_message(highlighted)

            intro = (
                f"🔍 <b>Натиҷаи ҷустуҷӯ</b>\n\n"
                f"📖 <b>{poem['book_title']}</b>\n"
                f"📜 <b>{poem['volume_number']} - Бахши {poem['poem_id']}</b>\n"
                f"🔹 {poem['section_title']}\n\n"
            )

            for i, part in enumerate(text_parts):
                message_text = f"{intro}<pre>{part}</pre>"
                if i == len(text_parts) - 1:
                    message_text += f"\n\nID: {poem['poem_id']}"
                
                keyboard = [[
                    InlineKeyboardButton(f"{EMOJI['book']} Шеъри пурра", 
                        callback_data=f"full_{poem['poem_id']}_0_{poem['volume_number']}")
                ]]
                
                await send_message_safe(
                    update,
                    message_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
    except Exception as e:
        logger.error(f"Error in search: {e}")
        await send_message_safe(
            update,
            f"""{EMOJI['error']} <b>Хатогӣ</b>

Ҷустуҷӯ иҷро нашуд. Лутфан баъдтар аз нав кӯшиш кунед.""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['back']} Аз нав кӯшиш кунед", callback_data=f"search_{search_term}")],
                [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
            ]),
            parse_mode='HTML'
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text == f"{EMOJI['masnavi']} Маснавии Маънавӣ":
        await masnavi_info(update, context)
    elif text == f"{EMOJI['info']} Маълумот":
        await balkhi_info(update, context)
    elif text == f"{EMOJI['daily']} Мисраи рӯз":
        await daily_verse(update, context)
    elif text == f"{EMOJI['search']} Ҷустуҷӯ":
        await send_message_safe(
            update,
            f"""🔍 <b>Ҷустуҷӯ</b>

Лутфан калимаро пас аз /search ворид намоед.

Мисол: /search ишқ""",
            reply_markup=ReplyKeyboardMarkup([[f"{EMOJI['home']} Ба аввал"]], resize_keyboard=True),
            parse_mode='HTML'
        )
    elif text == f"{EMOJI['home']} Ба аввал":
        await start(update, context)
    elif text.startswith(f"{EMOJI['poem']} Бахши "):
        try:
            poem_id = int(text.split()[2])
            await send_poem(update, poem_id)
        except (IndexError, ValueError):
            await send_message_safe(
                update,
                f"""{EMOJI['error']} <b>Хатогӣ</b>

ID-и нодуруст.""",
                reply_markup=ReplyKeyboardMarkup([[f"{EMOJI['home']} Ба аввал"]], resize_keyboard=True),
                parse_mode='HTML'
            )
    else:
        await handle_invalid_input(update, context)

async def handle_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_message_safe(
        update,
        f"""{EMOJI['error']} <b>Хатогӣ</b>

Фармони номаълум. Лутфан аз тугмаҳои меню истифода баред ё бо фармони /search ҷустуҷӯ кунед.""",
        reply_markup=ReplyKeyboardMarkup([[f"{EMOJI['home']} Ба аввал"]], resize_keyboard=True),
        parse_mode='HTML'
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        logger.warning("Received a button callback without a query.")
        return

    await query.answer()
    data = query.data

    try:
        if data == "loading":
            return

        if data.startswith("full_poem_"):
            unique_id = int(data.split("_")[2])
            poem = db.execute_query(
                "SELECT * FROM poems WHERE unique_id = %s",
                (unique_id,),
                fetch=True
            )
            if poem:
                await send_poem(query, poem[0]['poem_id'], show_full=True)

        elif data.startswith("poem_"):
            parts = data.split("_")
            poem_id = int(parts[1])
            part = int(parts[2]) if len(parts) > 2 else 0
            current_daftar = parts[3] if len(parts) > 3 else None
            await send_poem(query, poem_id, show_full=True, part=part, current_daftar=current_daftar)

        elif data.startswith("full_"):
            parts = data.split("_")
            poem_id = int(parts[1])
            part = int(parts[2]) if len(parts) > 2 else 0
            current_daftar = parts[3] if len(parts) > 3 else None
            await send_poem(query, poem_id, show_full=True, part=part, current_daftar=current_daftar)

        elif data == "masnavi_info":
            await masnavi_info(query, context)

        elif data == "divan_info":
            await divan_info(query, context)

        elif data == "back_to_info":
            await balkhi_info(query, context)

        elif data == "back_to_start":
            await start(query, context)

        elif data == "unavailable_daftar":
            await query.answer("Ин дафтар айни ҳол дастрас нест", show_alert=True)

        elif data.startswith("back_to_daftar_"):
            daftar_name = data.split("_")[3]
            await show_poems_page(query, context, daftar_name, page=1)

        elif data.startswith("daftar_"):
            parts = data.split("_")
            daftar_name = parts[1]
            if len(parts) > 2:
                page = int(parts[2])
                await show_poems_page(query, context, daftar_name, page)
            else:
                await show_poems_page(query, context, daftar_name, page=1)

        elif data == "back_to_daftars":
            await masnavi_info(query, context)

        elif data == "daily_verse":
            await daily_verse(query, context)

        elif data.startswith("search_"):
            search_term = data[7:]
            context.args = search_term.split()
            await search(query, context)

        elif data == "search_again":
            await send_message_safe(
                query,
                f"""🔍 <b>Ҷустуҷӯ</b>

Лутфан калимаро пас аз /search ворид намоед.

Мисол: /search ишқ""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
                ]),
                parse_mode='HTML'
            )

    except Exception as e:
        logger.error(f"Error in button_callback: {e}")
        await query.answer("Хатоги дар коркарди фармонат рух дод. Лутфан аз нав кӯшиш кунед.")
        await send_message_safe(
            query,
            f"""{EMOJI['error']} <b>Хатогӣ</b>

Коркарди фармон иҷро нашуд.""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Ба аввал", callback_data="back_to_start")]
            ]),
            parse_mode='HTML'
        )

async def highlight_verse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text(f"{EMOJI['error']} Шумо иҷозати иҷрои ин фармонро надоред.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            f"""⚙️ <b>Истифодаи фармон</b>

/highlight <unique_id> <матни мисра>

Барои сатрҳои нав, аз '||' истифода баред.""",
            parse_mode='HTML'
        )
        return

    try:
        poem_unique_id = int(context.args[0])
        verse_text = ' '.join(context.args[1:])
        verse_text = verse_text.replace('||', '\n')

        if db.is_highlight_exists(poem_unique_id, verse_text):
            await update.message.reply_text(
                f"{EMOJI['error']} Ин мисра аллакай дар ҳайати мисраҳои интихобшуда мавҷуд аст.",
                parse_mode='HTML'
            )
            return

        db.add_highlighted_verse(poem_unique_id, verse_text)
        await update.message.reply_text(
            f"""{EMOJI['success']} <b>Мисра илова шуд</b>

<pre>{verse_text}</pre>""",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Error adding highlighted verse: {e}")
        await update.message.reply_text(
            f"{EMOJI['error']} Хатогӣ дар иловаи мисра. Лутфан ID-и дурустро ворид кунед.",
            parse_mode='HTML'
        )

async def delete_highlight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text(f"{EMOJI['error']} Шумо иҷозати иҷрои ин фармонро надоред.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            f"""⚙️ <b>Истифодаи фармон</b>

/delete_highlight <highlight_id>""",
            parse_mode='HTML'
        )
        return

    try:
        highlight_id = int(context.args[0])
        db.delete_highlighted_verse(highlight_id)
        await update.message.reply_text(f"{EMOJI['success']} Мисраи бо ID {highlight_id} ҳазф шуд.")
    except Exception as e:
        logger.error(f"Error deleting highlighted verse: {e}")
        await update.message.reply_text(f"{EMOJI['error']} Хатогӣ дар ҳазфи мисра.")

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
    application.add_handler(CommandHandler("highlight", highlight_verse))
    application.add_handler(CommandHandler("delete_highlight", delete_highlight))

    # Message handlers
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_text))

    # Other content types handler
    application.add_handler(MessageHandler(
        filters.ALL & ~filters.TEXT & ~filters.COMMAND,
        handle_invalid_input))

    # Callback handlers
    application.add_handler(CallbackQueryHandler(button_callback))

    # Start the bot
    logger.info("Starting bot...")
    application.run_polling()

if __name__ == '__main__':
    main()
