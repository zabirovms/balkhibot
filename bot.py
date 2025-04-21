import logging
import os
import re
import time
import psycopg2
from psycopg2.extras import DictCursor
from telegram import ReplyKeyboardMarkup, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# Logging Setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
def get_config():
    config = {
        'DATABASE_URL': os.getenv('DATABASE_URL'),
        'BOT_TOKEN': os.getenv('BOT_TOKEN')
    }
    
    if not config['BOT_TOKEN']:
        logger.error("Bot token not configured!")
        raise ValueError("You must set BOT_TOKEN environment variable")
    
    if not config['DATABASE_URL']:
        logger.error("Database URL not configured!")
        raise ValueError("You must set DATABASE_URL environment variable")
    
    return config

# Database Manager Class
class DatabaseManager:
    def __init__(self, database_url, max_retries=3, retry_delay=2):
        self.conn = None
        self.database_url = database_url
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.connect_with_retry()

    def connect_with_retry(self):
        for attempt in range(self.max_retries):
            try:
                self.conn = psycopg2.connect(self.database_url, sslmode='require')
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

# ================== BALKHI INFORMATION SECTION ==================
async def balkhi_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info_text = """
<b>Мавлоно Ҷалолуддини Балхии Румӣ (30 сентябри 1207 — 17 декабри 1273)</b>

Мавлоно яке аз барҷастатарин мутафаккирон, орифон ва адибони сермаҳсули форсу тоҷик ба шумор меравад. Осори гаронбаҳои манзуми ӯ ба монанди «Девони кабир» (Куллиёти Шамси Табрезӣ) бо беш аз 40,000 байт, «Маснавии маънавӣ» бо тақрибан 26,000 байт, «Маҷолиси сабъа», «Фиҳи мо фиҳ» ва «Мактубот» то имрӯз ба дасти мо расида, дар хидмати ҷомиъаи фарҳангӣ қарор доранд.

Аз миёни ҳамаи осори ӯ, «Маснавии маънавӣ» ба унвони шоҳасари адабиёти форсу тоҷик ва пурарзиштарин ганҷи маънавии ирфонӣ шинохта шудааст. Ин асарро аз замони эҷодаш то имрӯз ҳамчун тарҷумаи Қуръон бо забони форсӣ шинохта, мавқеи баланди онро дар таърихи илму адаб ва маънавиёт таъкид намудаанд.

Ҳанӯз дар асри XVI шоир ва донишманди бузург Шайх Баҳоӣ «Маснавиро» чунин тавсиф кард:

<i>Ман намегӯям, ки он олиҷаноб — 
Ҳаст пайғамбар, вале дорад Китоб.
«Маснавии маънавӣ»-и Мавлавӣ — 
Ҳаст Қуръоне ба лафзи паҳлавӣ.</i>

Шахсиятҳои маъруф, чун Аллома Иқболи Лоҳурӣ, Мавлоноро чун муршиди равшанзамир ситоиш кардаанд:

<i>Пири Румӣ, муршиди равшанзамир,
Корвони ишқу мастиро амир.
Нури Қуръон дар миёни синааш,
Ҷоми Ҷам шарманда аз ойинааш.</i>

Эҷоди «Маснавӣ» бо ташвиқи Ҳусомуддини Чалабӣ — муриди содиқ ва ёвари наздики Мавлоно сурат гирифтааст. Ҳусамуддин бо хоҳиши худ Мавлоноро ба навиштани ин асари бузург ташвиқ кард. Беш аз даҳ сол Мавлоно дар шакли дафтарҳо ин асарро бадоҳатан эҷод намуда, шогирдон онро китобат мекарданд.

«Маснавӣ» дар вазни рамали мусаддаси маҳзуф (фоилотун, фоилотун, фоилун) навишта шуда, дар баробари масоили ирфонӣ, андешаҳои фалсафӣ, иҷтимоӣ, мазҳабӣ ва ахлоқиро фаро мегирад.

Мероси Мавлоно то имрӯз сарчашмаи илҳом барои дӯстдорони адаб, ирфон ва инсонгароӣ мебошад.
"""
    
    keyboard = [
        [InlineKeyboardButton("Маснавии Маънавӣ", callback_data="masnavi_info")],
        [InlineKeyboardButton("Девони Шамс", callback_data="divan_info")],
        [InlineKeyboardButton("Ба аввал", callback_data="back_to_start")]
    ]
    
    await send_message_safe(
        update,
        info_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def masnavi_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info_text = """
<b>Маснавии Маънавӣ</b>

Шоҳасари адабиёти ирфонӣ дорои 6 дафтар ва 26,000 байт. Мавзӯҳои марказии асар:

- Ҳикояҳои панду ахлоқӣ
- Нуктаҳои фалсафӣ ва ирфонӣ
- Шарҳи оёти Қуръон ва аҳодис
- Таълимоти иҷтимоӣ ва инсонии

"Маснавии маънавӣ зиндагиро бо ҳама шодию ғам, бешу кам, шӯру шар, барору нобарориҳо таҷассум намудааст."
"""
    keyboard = [
        [InlineKeyboardButton("Ба қафо", callback_data="back_to_info")],
        [InlineKeyboardButton("Ба аввал", callback_data="back_to_start")]
    ]
    
    await send_message_safe(
        update,
        info_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def divan_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info_text = """
<b>Девони Шамс ё Девони Кабир</b>

Дорои:
- 3,200 ғазал
- 2,000 рубоӣ
- Қасидаҳо ва тарҷеъот

<i>Аз муҳаббат талхҳо ширин шавад,
Аз муҳаббат миссҳо заррин шавад.</i>
"""
    keyboard = [
        [InlineKeyboardButton("Ба қафо", callback_data="back_to_info")],
        [InlineKeyboardButton("Ба аввал", callback_data="back_to_start")]
    ]
    
    await send_message_safe(
        update,
        info_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
# ================== END BALKHI INFORMATION SECTION ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["Маснавии Маънавӣ"],
        ["Девони Шамс"],
        ["Фиҳӣ Мо Фиҳ", "Маҷолиси Сабъа"],
        ["Макотиб"],
        ["Ҷустуҷӯ", "Маълумот дар бораи Балхӣ"]
    ]
    await send_message_safe(
        update,
        "Аз тугмачаҳои зер асарҳои мавриди назаратонро кушода мутолиа кунед ё бо истифода аз фармони **/search 'калимаи мехостаатон'** шеъри дилхоҳатонро ёбед:",
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
            InlineKeyboardButton("📖 Пурра дидан", callback_data=f"full_{poem_id}_0")
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
            "Маснавии Маънавӣ: Лутфан интихоб кунед",
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

    elif text == "Маъолиcи Сабъа":
        description = "Маъолиҷи Сабъа:\nНавъ: Наср\nШарҳ: Ҳафт маҷлиси маърифатӣ ва иршодӣ аз Балхӣ.\n\nАйни ҳол дастрас нест"
        await send_message_safe(update, description, reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Макотиб":
        description = "Макотиб:\nНавъ: Номаҳо\nШарҳ: Маҷмӯаи номаҳои шахсии Балхӣ ба дӯстону муридон.\n\nАйни ҳол дастрас нест"
        await send_message_safe(update, description, reply_markup=ReplyKeyboardMarkup([["Ба аввал"]], resize_keyboard=True))

    elif text == "Маълумот дар бораи Балхӣ":
        await balkhi_info(update, context)

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
    elif data == "masnavi_info":
        await masnavi_info(query, context)
    elif data == "divan_info":
        await divan_info(query, context)
    elif data == "back_to_info":
        await balkhi_info(query, context)
    elif data == "back_to_start":
        await start(query, context)

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
    try:
        # Load configuration
        config = get_config()
        
        # Initialize database connection
        global db
        db = DatabaseManager(config['DATABASE_URL'])
        
        # Create application
        application = Application.builder().token(config['BOT_TOKEN']).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("search", search))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        application.add_handler(CallbackQueryHandler(button_callback))
        
        logger.info("Starting bot...")
        application.run_polling()
        
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
    except Exception as e:
        logger.error(f"Application error: {e}")
    finally:
        if 'db' in globals() and db.conn:
            db.close()
            logger.info("Database connection closed.")

if __name__ == '__main__':
    main()
