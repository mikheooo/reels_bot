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
    dp = Dispatcher()
    dp.include_router(router)
    
    logging.info("Starting bot...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Bot stopped/crashed (likely invalid token): {e}")

if __name__ == "__main__":
    asyncio.run(main())
