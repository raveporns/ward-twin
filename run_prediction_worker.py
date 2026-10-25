
import logging
import signal
import time

from app.services.prediction_worker import run_once

POLL_INTERVAL_SECONDS = 5
ERROR_RETRY_SECONDS = 10

running = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("prediction-worker")


def handle_shutdown(signum, frame):
    global running
    logger.info("Shutdown requested")
    running = False


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def main():
    logger.info("Automatic Prediction Worker started")

    while running:
        try:
            result = run_once()

            if result["status"] == "processed":
                logger.info(
                    "Processed encounter=%s prediction_id=%s "
                    "prediction_status=%s",
                    result["encounter_id"],
                    result["prediction_id"],
                    result["prediction_status"],
                )

                # ถ้ามีงานค้าง ตรวจงานถัดไปทันที
                continue

            if result["status"] == "no_jobs":
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            logger.warning("Unexpected result: %s", result)
            time.sleep(POLL_INTERVAL_SECONDS)

        except Exception:
            logger.exception("Prediction Worker failed")
            time.sleep(ERROR_RETRY_SECONDS)

    logger.info("Automatic Prediction Worker stopped")


if __name__ == "__main__":
    main()
