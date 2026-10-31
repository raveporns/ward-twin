
import os
import json
import time
import uuid
from datetime import datetime, timezone

import requests
import serial
from serial.tools import list_ports


API_BASE_URL = os.getenv(
    "WARD_TWIN_API_URL",
    "https://ward-twin.onrender.com"
).rstrip("/")

GATEWAY_KEY = os.getenv("WARD_TWIN_GATEWAY_KEY", "")
ENCOUNTER_ID = os.getenv("WARD_TWIN_ENCOUNTER_ID", "")
DEVICE_ID = os.getenv("WIO_DEVICE_ID", "WIO-TERMINAL-01")
SERIAL_PORT = os.getenv("WIO_SERIAL_PORT", "")
BAUD_RATE = 115200


def find_wio_port():
    if SERIAL_PORT:
        return SERIAL_PORT

    ports = [
        p.device
        for p in list_ports.comports()
        if "usbmodem" in p.device.lower()
    ]

    if not ports:
        raise RuntimeError("Wio Terminal not found")

    return ports[0]


def read_wio_data(line):
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None

    required = ("heart_rate", "respiratory_rate", "spo2")

    if not isinstance(data, dict):
        return None

    if not all(key in data for key in required):
        return None

    return data


def build_observation(data):
    if not ENCOUNTER_ID:
        raise RuntimeError(
            "WARD_TWIN_ENCOUNTER_ID is not configured"
        )

    return {
        "source": "iot",
        "readings": [
            {
                "reading_id": str(uuid.uuid4()),
                "encounter_id": ENCOUNTER_ID,
                "device_id": DEVICE_ID,
                "measured_at": datetime.now(
                    timezone.utc
                ).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "measurements": [
                    {
                        "metric": "heart_rate",
                        "value": data["heart_rate"],
                        "unit": "bpm",
                    },
                    {
                        "metric": "respiratory_rate",
                        "value": data["respiratory_rate"],
                        "unit": "breaths/min",
                    },
                    {
                        "metric": "spo2",
                        "value": data["spo2"],
                        "unit": "%",
                    },
                ],
            }
        ],
    }



def send_observation(payload):
    if not GATEWAY_KEY:
        raise RuntimeError(
            "WARD_TWIN_GATEWAY_KEY is not configured"
        )

    response = requests.post(
        f"{API_BASE_URL}/api/v1/observations",
        json=payload,
        headers={
            "X-Gateway-Key": GATEWAY_KEY,
            "Content-Type": "application/json",
        },
        timeout=(10, 60),
    )

    print("HTTP Status:", response.status_code)

    try:
        print(
            "Response:",
            json.dumps(
                response.json(),
                indent=2,
                ensure_ascii=False
            )
        )
    except ValueError:
        print("Response:", response.text)

    response.raise_for_status()
    return response



def main():
    if not ENCOUNTER_ID:
        raise RuntimeError(
            "Set WARD_TWIN_ENCOUNTER_ID first"
        )

    if not GATEWAY_KEY:
        raise RuntimeError(
            "Set WARD_TWIN_GATEWAY_KEY first"
        )

    port = find_wio_port()

    print("=== WARD TWIN WIO GATEWAY ===")
    print("Serial Port:", port)
    print("Backend:", API_BASE_URL)
    print("Encounter:", ENCOUNTER_ID)
    print("Device:", DEVICE_ID)

    with serial.Serial(port, BAUD_RATE, timeout=2) as device:
        time.sleep(2)

        while True:
            raw = device.readline().decode(
                "utf-8", errors="replace"
            ).strip()

            if not raw:
                continue

            data = read_wio_data(raw)

            if data is None:
                print("Ignored:", raw)
                continue

            print("Received:", data)

            payload = build_observation(data)
            print(
                "Sending:",
                json.dumps(payload, indent=2)
            )

            try:
                send_observation(payload)
                print("POST successful")
            except requests.RequestException as error:
                print("POST failed:", error)

            break  # ทดสอบส่งเพียง 1 Payload


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nGateway stopped")
    except (RuntimeError, serial.SerialException) as error:
        print("Gateway error:", error)
