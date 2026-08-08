import os
import asyncio

from arq import create_pool
from arq.connections import RedisSettings


async def main():
    chat_id = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
    redis = await create_pool(RedisSettings(host='localhost', port=6379))
    await redis.enqueue_job('process_video', 'b19a37a4-4ae0-4f2b-a776-0986fb908b03', 'https://www.instagram.com/reel/DbTfh6PxJUr/?igsh=NHV2NnU2NTFjanRh', chat_id)
    print("Job enqueued!")

asyncio.run(main())
