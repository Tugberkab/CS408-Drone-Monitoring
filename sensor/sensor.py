# sensor/sensor.py
"""
Headless sensor client
----------------------
Usage examples
    # normal:
    python -m sensor.sensor --id s1 --interval 2 --drone-port 5000

    # inject one extreme temperature reading (1000 °C) then resume normal:
    python -m sensor.sensor --id bad --spike-temp 1000

    # inject one humidity spike:
    python -m sensor.sensor --spike-hum -10
"""

import asyncio
import random
import argparse
import logging
from common.message import SensorReading, iso_now


async def run_sensor(cfg: argparse.Namespace) -> None:
    """Reconnect-forever loop; sends JSON every `interval` secoxnds."""
    while True:
        try:
            reader, writer = await asyncio.open_connection(cfg.drone_ip, cfg.drone_port)
            log_message(f"✅ Sensor {cfg.id} connected to drone")
            first_loop = True          # track when to send spike

            while not writer.is_closing():
                # normal random reading
                msg = SensorReading(
                    sensor_id=cfg.id,
                    temperature=round(random.uniform(20, 30), 1),
                    humidity=round(random.uniform(40, 60), 1),
                    timestamp=iso_now(),
                )
                await _send(writer, msg)

                # optional one-shot spike
                if first_loop:
                    if cfg.spike_temp is not None:
                        msg.temperature = cfg.spike_temp
                        await _send(writer, msg)
                        cfg.spike_temp = None
                    if cfg.spike_hum is not None:
                        msg.humidity = cfg.spike_hum
                        await _send(writer, msg)
                        cfg.spike_hum = None
                    first_loop = False

                await asyncio.sleep(cfg.interval)

        except (OSError, ConnectionError) as e:
            log_message(f"❌ Sensor {cfg.id} connection failed – retrying in 5 s")
            await asyncio.sleep(5)
        except Exception as e:
            log_message(f"⚠️ Unexpected error in sensor {cfg.id}: {e}")
            await asyncio.sleep(5)


async def _send(writer: asyncio.StreamWriter, reading: SensorReading) -> None:
    try:
        writer.write(reading.to_json())
        await writer.drain()
    except Exception as e:
        logging.warning(f"⚠️ Failed to send reading: {e}")


def log_message(msg: str) -> None:
    """Unified logging for both console and Drone/Central GUIs."""
    stamp = iso_now()
    formatted_msg = f"[{stamp}] {msg}"
    print(formatted_msg)  # Console log


# ─────────────────────────── CLI ────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--drone-ip",   default="127.0.0.1")
    parser.add_argument("--drone-port", type=int, default=5000)
    parser.add_argument("--interval",   type=int, default=2)
    parser.add_argument("--id", default=f"sensor-{random.randint(1000,9999)}")
    parser.add_argument("--spike-temp", type=float, default=None,
                        help="send ONE extreme temperature reading then resume normal")
    parser.add_argument("--spike-hum",  type=float, default=None,
                        help="send ONE extreme humidity reading then resume normal")
    cfg = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")

    asyncio.run(run_sensor(cfg))
