# drone/drone_server.py
"""
Drone edge node  – Phase 3
~~~~~~~~~~~~~~~~~~~~~~~~~~
• Async TCP **server** for sensor nodes
• Async TCP **client** to the Central Server
• Rolling averages, anomaly detection, battery simulation
"""

from __future__ import annotations
from datetime import datetime
import asyncio, json, argparse, logging, time
from collections import defaultdict, deque
from statistics import mean

# ───────── configuration ─────────
ROLLING_SIZE       = 10          # readings kept per sensor
FORWARD_INTERVAL   = 5           # seconds between pushes to Central
ANOMALY_TEMP_RANGE = (0, 60)     # °C
ANOMALY_HUM_RANGE  = (0, 100)    # %
# ─────────────────────────────────

class DroneServer:
    def __init__(self,
                 listen_port: int,
                 central_ip: str,
                 central_port: int,
                 drone_id: str = "drone-1") -> None:

        self.listen_port   = listen_port
        self.central_ip    = central_ip
        self.central_port  = central_port
        self.drone_id      = drone_id

        self.store = defaultdict(lambda: deque(maxlen=ROLLING_SIZE))
        self._pending_events: list[dict] = []     # sensor_disconnect etc.

        # battery
        from drone.battery import Battery
        self.battery   = Battery(capacity=100, drain_rate=2.0)  # % per minute
        self.returning = False

        # Central TCP client state
        self._central_reader: asyncio.StreamReader | None = None
        self._central_writer: asyncio.StreamWriter | None = None
        self._central_lock   = asyncio.Lock()

    # ───────────────── sensor-side TCP server ──────────────────
    def _log_msg(self, msg: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        line  = f"{stamp} | {msg}"
        logging.info(line)
        if self.with_gui and self._log:
            def _append():
                self._log.configure(state="normal")
                self._log.insert("end", line + "\n")
                self._log.see("end")
                self._log.configure(state="disabled")
            self._root.after(0, _append)

    async def handle_sensor(self, reader, writer):
        peer = writer.get_extra_info("peername")
        sid = None

        logging.info(f"↪️  Sensor connected from {peer}")

        try:
            while line := await reader.readline():
                data = json.loads(line)
                sid = data["sensor_id"]
                self.store[sid].append(data)
                logging.info(f" 📥 {sid}: {data['temperature']} °C  {data['humidity']} %")
        except asyncio.IncompleteReadError:
            pass
        finally:
            if sid:
                # Log the disconnection as an anomaly
                anomaly = {
                    "sensor_id": sid,
                    "type": "disconnection",
                    "value": "Sensor disconnected",
                    "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                }
                self.anomalies.append(anomaly)
                logging.warning(f"❌ Sensor {sid} disconnected")
            writer.close(); await writer.wait_closed()


    async def start_sensor_server(self) -> None:
        server = await asyncio.start_server(
            self.handle_sensor, host="127.0.0.1", port=self.listen_port)
        addr = ", ".join(str(sock.getsockname()) for sock in server.sockets)
        logging.info(f"🛰️  Drone listening for sensors on {addr}")
        async with server:
            await server.serve_forever()

    # ───────────────── Central-side TCP client ────────────────
    async def _connect_central(self) -> asyncio.StreamWriter:
        while True:
            try:
                reader, writer = await asyncio.open_connection(
                    self.central_ip, self.central_port)
                self._central_reader, self._central_writer = reader, writer
                logging.info("✅ Connected to Central Server")
                return writer
            except OSError as e:
                logging.error(f"Central down ({e}); retrying in 3 s…")
                await asyncio.sleep(3)

    async def _get_central_writer(self) -> asyncio.StreamWriter:
        async with self._central_lock:
            if (self._central_writer is None or
                    self._central_writer.is_closing()):
                self._central_writer = await self._connect_central()
            return self._central_writer

    # ───────────────── summary builder ────────────────────────
    def _build_summary(self) -> dict:
        newest = [dq[-1] for dq in self.store.values() if dq]

        anomalies: list[dict] = []
        if newest:
            avg_temp = mean(r["temperature"] for r in newest)
            avg_hum  = mean(r["humidity"]    for r in newest)
            
            latest_temp = newest[-1]["temperature"]   # newest is already built
            latest_hum  = newest[-1]["humidity"]
            for r in newest:
                if not (ANOMALY_TEMP_RANGE[0] <= r["temperature"] <= ANOMALY_TEMP_RANGE[1]):
                    anomalies.append({
                        "sensor_id": r["sensor_id"],
                        "type":      "temperature",
                        "value":     r["temperature"],
                        "timestamp": r["timestamp"]
                    })
                if not (ANOMALY_HUM_RANGE[0]  <= r["humidity"]    <= ANOMALY_HUM_RANGE[1]):
                    anomalies.append({
                        "sensor_id": r["sensor_id"],
                        "type":      "humidity",
                        "value":     r["humidity"],
                        "timestamp": r["timestamp"]
                    })
        else:
            avg_temp = avg_hum = None  # no live sensors

        # battery-low anomaly once per returning episode
        if self.returning and not any(a["type"] == "battery_low" for a in anomalies):
            anomalies.append({
                "sensor_id": "-",
                "type":      "battery_low",
                "value":     round(self.battery.as_percent(), 1),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            })

        # merge queued events (e.g. sensor_disconnect)
        if self._pending_events:
            anomalies.extend(self._pending_events)
            self._pending_events.clear()

        # if there is *nothing* to report yet, return {}
        if not newest and not anomalies:
            return {}

        return {
            "drone_id": self.drone_id,
            "battery":  round(self.battery.as_percent(), 1),
            "average_temperature": round(avg_temp, 2),
            "average_humidity":    round(avg_hum, 2),
            "latest_temperature":  latest_temp,     # ← NEW
            "latest_humidity":     latest_hum,      # ← NEW
            "anomalies": anomalies,
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        }


    # ───────────────── main forward loop ──────────────────────
    async def forward_loop(self) -> None:
        while True:
            await asyncio.sleep(FORWARD_INTERVAL)
            self.battery.tick()

            # returning <--> normal transition
            if self.battery.level < 20 and not self.returning:
                self.returning = True
                logging.warning("🔋 Battery low (<20 %) – returning")
            elif self.battery.level >= 20 and self.returning:
                self.returning = False
                logging.info("🔋 Battery OK – resuming normal operation")

            payload = self._build_summary()
            if not payload:
                continue  # nothing to send yet

            if self.returning:
                payload.pop("average_temperature", None)
                payload.pop("average_humidity",   None)
                payload["returning"] = True
            else:
                payload["returning"] = False

            try:
                w = await self._get_central_writer()
                w.write(json.dumps(payload).encode() + b"\n")
                await w.drain()
            except (ConnectionResetError, OSError):
                logging.error("Lost connection to Central; will reconnect")
                if self._central_writer:
                    self._central_writer.close()
                self._central_writer = None

    # optional: listen for control messages (drain / recharge) ----------
    async def listen_central(self):
        while True:
            if not self._central_reader:
                await asyncio.sleep(1)
                continue
            try:
                line = await self._central_reader.readline()
                if not line:
                    self._central_reader = None
                    continue
                msg = json.loads(line)
                if (msg.get("type") == "battery"
                        and msg.get("target") == self.drone_id):
                    action = msg["action"]
                    if action == "drain":
                        self.battery.level = max(0, self.battery.level - msg.get("amount", 0))
                        logging.warning("🪫 battery drained via control message")
                    elif action == "recharge":
                        self.battery.charge()
                        logging.info("🔌 battery recharged via control message")
            except (asyncio.IncompleteReadError, ConnectionResetError, json.JSONDecodeError):
                self._central_reader = None

# ─────────────────── standalone entry point ───────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Drone edge node")
    p.add_argument("--listen-port",  type=int, default=5000)
    p.add_argument("--central-ip",   default="127.0.0.1")
    p.add_argument("--central-port", type=int, default=6000)
    p.add_argument("--id",           default="drone-1")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-8s | %(message)s",
                        datefmt="%H:%M:%S")

    drone = DroneServer(args.listen_port, args.central_ip, args.central_port, args.id)

    async def main():
        await asyncio.gather(
            drone.start_sensor_server(),
            drone.forward_loop(),
            drone.listen_central()
        )
    asyncio.run(main())
