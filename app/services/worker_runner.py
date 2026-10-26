import asyncio
import logging

from app.services.prediction_worker import run_once

logger = logging.getLogger(__name__)

POLL_SECONDS = 5


async def prediction_worker_loop():
    logger.info("Prediction worker started")

    while True:
        try:
            # run_once() เป็น synchronous และใช้ SQLAlchemy Session
            # จึงแยกไปทำงานใน thread ไม่ให้ block FastAPI
            result = await asyncio.to_thread(run_once)

            if result["status"] == "processed":
                logger.info(
                    "Prediction processed: %s",
                    result,
                )
                # มีงานอื่นค้างอยู่หรือไม่ ให้ตรวจต่อทันที
                continue

            await asyncio.sleep(POLL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Prediction worker stopping")
            raise

        except Exception:
            logger.exception("Prediction worker failed")
            await asyncio.sleep(POLL_SECONDS)