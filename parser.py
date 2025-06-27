import asyncio
import re
import sqlite3
import os
import logging
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityUrl
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from pymorphy2 import MorphAnalyzer

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Загрузка NLP ресурсов
nltk.download('punkt', quiet=True)
nltk.download('stopwords', quiet=True)

# Конфигурация
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHECK_INTERVAL = 300  # 5 минут

# Инициализация
morph = MorphAnalyzer()
russian_stopwords = stopwords.words('russian')
client = TelegramClient('news_parser', API_ID, API_HASH).start(bot_token=BOT_TOKEN)


# База данных
def init_db():
    conn = sqlite3.connect('news_bot.db')
    c = conn.cursor()

    # Таблица пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (
                     user_id
                     INTEGER
                     PRIMARY
                     KEY,
                     created_at
                     TIMESTAMP
                     DEFAULT
                     CURRENT_TIMESTAMP
                 )''')

    # Таблица каналов
    c.execute('''CREATE TABLE IF NOT EXISTS channels
    (
        id
        INTEGER
        PRIMARY
        KEY
        AUTOINCREMENT,
        url
        TEXT
        NOT
        NULL
        UNIQUE,
        user_id
        INTEGER
        NOT
        NULL,
        last_parsed_id
        INTEGER
        DEFAULT
        0,
        FOREIGN
        KEY
                 (
        user_id
                 ) REFERENCES users
                 (
                     user_id
                 )
        )''')

    # Ограничение: максимум 10 каналов на пользователя
    c.execute('''
              CREATE TRIGGER IF NOT EXISTS channel_limit
        BEFORE INSERT ON channels
        FOR EACH ROW
        WHEN (SELECT COUNT(*) FROM channels WHERE user_id = NEW.user_id) >= 10
              BEGIN
              SELECT RAISE(ABORT, 'Channel limit reached');
              END;
              ''')

    conn.commit()
    return conn


db = init_db()


# Очистка текста
def clean_text(text):
    # Удаление ненужных элементов
    text = re.sub(r'http\S+', '', text)  # Ссылки
    text = re.sub(r'@\w+', '', text)  # Упоминания
    text = re.sub(r'#\w+', '', text)  # Хештеги
    text = re.sub(r'Реклама:.*', '', text, flags=re.IGNORECASE)  # Реклама
    return text.strip()


# Упрощение текста
def simplify_text(text):
    tokens = word_tokenize(text, language="russian")
    cleaned_tokens = [
        morph.parse(token)[0].normal_form
        for token in tokens
        if token.isalnum() and token not in russian_stopwords
    ]
    return ' '.join(cleaned_tokens)


# Обработка команд
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    user_id = event.sender_id
    try:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        db.commit()

        await event.reply(
            "📰 Привет! Я парсер новостей.\n\n"
            "Добавь каналы командой:\n"
            "/add https://t.me/example\n\n"
            "Управление каналами:\n"
            "/list - список каналов\n"
            "/remove - удалить канал\n\n"
            "Бот проверяет новые посты каждые 5 минут"
        )
    except Exception as e:
        logger.error(f"Ошибка в /start: {e}")
        await event.reply("⚠️ Произошла ошибка. Попробуйте позже.")


@client.on(events.NewMessage(pattern='/add'))
async def add_channel(event):
    user_id = event.sender_id
    try:
        match = re.search(r'https://t\.me/\w+', event.text)
        if not match:
            await event.reply("❌ Неверный формат. Пример: /add https://t.me/example")
            return

        url = match.group(0)
        c = db.cursor()

        try:
            c.execute("INSERT INTO channels (url, user_id) VALUES (?, ?)", (url, user_id))
            db.commit()
            await event.reply(f"✅ Канал {url} добавлен!")
        except sqlite3.IntegrityError:
            await event.reply("⚠️ Канал уже в списке")
        except sqlite3.OperationalError as e:
            if "channel_limit" in str(e):
                await event.reply("🚫 Лимит: не более 10 каналов")
            else:
                raise

    except Exception as e:
        logger.error(f"Ошибка в /add: {e}")
        await event.reply("⚠️ Ошибка при добавлении канала")


@client.on(events.NewMessage(pattern='/remove'))
async def remove_channel(event):
    user_id = event.sender_id
    try:
        match = re.search(r'https://t\.me/\w+', event.text)
        if not match:
            await event.reply("❌ Неверный формат. Пример: /remove https://t.me/example")
            return

        url = match.group(0)
        c = db.cursor()
        c.execute("DELETE FROM channels WHERE url = ? AND user_id = ?", (url, user_id))
        db.commit()

        if c.rowcount > 0:
            await event.reply(f"🗑 Канал {url} удален")
        else:
            await event.reply("ℹ️ Канал не найден")

    except Exception as e:
        logger.error(f"Ошибка в /remove: {e}")
        await event.reply("⚠️ Ошибка при удалении канала")


@client.on(events.NewMessage(pattern='/list'))
async def list_channels(event):
    user_id = event.sender_id
    try:
        c = db.cursor()
        c.execute("SELECT url FROM channels WHERE user_id = ?", (user_id,))
        channels = c.fetchall()

        if not channels:
            await event.reply("📭 Нет добавленных каналов")
            return

        response = "📋 Ваши каналы:\n\n" + "\n".join([f"• {ch[0]}" for ch in channels])
        await event.reply(response)

    except Exception as e:
        logger.error(f"Ошибка в /list: {e}")
        await event.reply("⚠️ Ошибка при получении списка")


# Парсинг каналов
async def parse_channels():
    while True:
        try:
            c = db.cursor()
            c.execute("SELECT url, user_id, last_parsed_id FROM channels")
            channels = c.fetchall()

            for url, user_id, last_parsed_id in channels:
                try:
                    entity = await client.get_entity(url)
                    messages = await client.get_messages(entity, limit=5, min_id=last_parsed_id)

                    if not messages:
                        continue

                    new_last_id = max(m.id for m in messages)

                    for message in messages:
                        if message.id <= last_parsed_id or not message.text:
                            continue

                        # Обработка текста
                        cleaned_text = clean_text(message.text)
                        simplified_text = simplify_text(cleaned_text)

                        if not simplified_text:
                            continue

                        # Формирование ответа
                        response = (
                            f"📰 [Обработанная новость]\n"
                            f"➖➖➖\n"
                            f"{simplified_text}\n"
                            f"➖➖➖\n"
                            f"Источник: {entity.title}"
                        )

                        # Отправка пользователю
                        await client.send_message(
                            user_id,
                            response,
                            buttons=[
                                [{"text": "✅ Опубликовать", "data": "publish"}],
                                [{"text": "❌ Пропустить", "data": "skip"}]
                            ]
                        )

                        # Обновление последнего ID
                        c.execute(
                            "UPDATE channels SET last_parsed_id = ? WHERE url = ?",
                            (new_last_id, url)
                        )
                        db.commit()

                except Exception as e:
                    logger.error(f"Ошибка парсинга {url}: {e}")

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.error(f"Ошибка в парсере: {e}")
            await asyncio.sleep(60)


# Обработка кнопок
@client.on(events.CallbackQuery)
async def handle_buttons(event):
    try:
        if event.data == b'publish':
            await event.answer("Текст скопирован в буфер")
            # Здесь можно добавить функционал копирования
        elif event.data == b'skip':
            await event.delete()

    except Exception as e:
        logger.error(f"Ошибка обработки кнопки: {e}")


# Запуск
async def main():
    await client.start()
    logger.info("Бот запущен!")
    asyncio.create_task(parse_channels())
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())