import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot.handlers import router
from app.core.config import settings
from app.db.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def main():
    await init_db()
    bot = Bot(token=settings.bot_token)
    try:
        me = await bot.get_me()
        expected = (settings.expected_bot_username or "").lstrip("@").lower()
        actual = (me.username or "").lower()
        if expected and actual != expected:
            raise RuntimeError(
                f"Telegram bot identity mismatch: expected @{expected}, got @{actual or 'no_username'}"
            )
        logging.info("Telegram identity verified: @%s (%s)", me.username, me.id)

        dp = Dispatcher()
        dp.include_router(router)
        
        logging.info("Starting bot...")
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Bot stopped/crashed (likely invalid token or identity mismatch): {e}")
        raise
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
