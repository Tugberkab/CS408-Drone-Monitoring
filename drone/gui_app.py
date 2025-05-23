"""
Drone GUI (Phase‑3)
------------------
Run me with:
    python -m drone.gui_app --listen-port 5000 \
                            --central-ip 127.0.0.1 --central-port 6000 \
                            --id drone-1

Features
~~~~~~~~
• Starts **DroneServer** in a background asyncio thread.
• Live table of raw sensor readings (last 100).
• Progress‑bar that tracks simulated battery (%).
• Real‑time labels for computed averages.
• Anomaly list (red) coming from summary packets.
• Button to drain battery by 5 %; button to recharge to 100 %.
• Scrollable log pane (receives Python logging records).

Dependencies: only Python ≥ 3.9 std‑lib (Tkinter, asyncio, logging, threading).
"""
from __future__ import annotations

from datetime import datetime
import argparse, asyncio, json, logging, threading, time
from collections import deque
from functools import partial

# ───────────────────── project imports ─────────────────────
from drone.drone_server import DroneServer           # phase‑2 core logic

# ───────────────────── Tkinter GUI ─────────────────────────
import tkinter as tk
from tkinter import ttk, scrolledtext


class DroneGUI:
    def __init__(self, cfg: argparse.Namespace):
        self.cfg = cfg
        self.drone = DroneServer(
            cfg.listen_port, cfg.central_ip, cfg.central_port, cfg.id
        )

        # ---------- Tk root ----------
        self.root = tk.Tk()
        self.root.title(f"Drone GUI – {cfg.id}")
        self.root.geometry("820x600")

        # ---------- raw readings table ----------
        cols = ("sensor", "temp", "hum", "ts")
        self.table = ttk.Treeview(self.root, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (80, 80, 80, 220)):
            self.table.heading(c, text=c)
            self.table.column(c, width=w, anchor="center")
        self.table.pack(fill="x", padx=6, pady=6)

        # ---------- averages + battery bar ----------
        frm_top = ttk.Frame(self.root); frm_top.pack(fill="x", padx=6)

        self.lbl_avg_t = ttk.Label(frm_top, text="Avg T: ––– °C", width=18)
        self.lbl_avg_h = ttk.Label(frm_top, text="Avg H: ––– %", width=18)
        self.lbl_avg_t.pack(side="left", padx=6)
        self.lbl_avg_h.pack(side="left", padx=6)

        self.lbl_batt = ttk.Label(frm_top, text="100 %")
        self.lbl_batt.pack(side="left", padx=(0,4))

        ttk.Label(frm_top, text="Battery").pack(side="left", padx=(10,4))
        self.b_prog = ttk.Progressbar(frm_top, length=150, maximum=100, mode="determinate")
        self.b_prog.pack(side="left")
        self.b_prog["value"] = 100

        self.btn_drain = ttk.Button(frm_top, text="Drain 5 %", command=self._drain5)
        self.btn_drain.pack(side="left", padx=6)
        self.btn_charge = ttk.Button(frm_top, text="Recharge", command=self._recharge)
        self.btn_charge.pack(side="left")

        # ---------- anomaly list ----------
        ttk.Label(self.root, text="Anomalies").pack(pady=(8,0))
        self.lst_anom = tk.Listbox(self.root, height=6)
        self.lst_anom.pack(fill="x", padx=6)

        # ---------- log pane ----------
        ttk.Label(self.root, text="Log").pack(pady=(8,0))
        self.txt_log = scrolledtext.ScrolledText(self.root, height=10, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=(0,6))

        # ---------- internal rolling buffer ----------
        self._latest_rows: deque[dict] = deque(maxlen=100)

    # =========================================================
    #   background thread – run DroneServer inside asyncio loop
    # =========================================================

    def start_background(self):
        def runner():
            asyncio.run(self._async_main())
        threading.Thread(target=runner, daemon=True, name="DroneServerThread").start()

    async def _async_main(self):
        orig_handle = self.drone.handle_sensor           # bound coroutine

        async def handle_sensor_hook(reader, writer):
            """Proxy for DroneServer.handle_sensor – we tap the line first,
            then call the original coroutine so the store / logging stays intact."""
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # still pass the raw line to the original handler
                    await orig_handle(reader, writer)
                    continue

                # --- 1) feed GUI ---
                self._on_raw(msg)

                # --- 2) feed DroneServer logic (rolling store + log) ---
                sid = msg["sensor_id"]
                self.drone.store[sid].append(msg)
                logging.info(f" 📥 {sid}: {msg['temperature']} °C  {msg['humidity']} %")

        # replace handler
        self.drone.handle_sensor = handle_sensor_hook

        # --- patch _build_summary to intercept aggregates ---
        orig_summary = self.drone._build_summary
        def summary_hook():
            s = orig_summary()
            if s:
                self._on_summary(s)
            return s
        self.drone._build_summary = summary_hook  # type: ignore

        # --- redirect logging to GUI ---
        class TkLogHandler(logging.Handler):
            def emit(inner, record):
                self._append_log(inner.format(record))
        log = logging.getLogger()
        log.addHandler(TkLogHandler())

        await asyncio.gather(
            self.drone.start_sensor_server(),
            self.drone.forward_loop()
        )

    # =========================================================
    #   GUI callbacks (called from async thread, marshalled via Tk)
    # =========================================================

    def _on_raw(self, msg: dict):
        def tk_add():
            self.table.insert("", "end", values=(
                msg["sensor_id"],
                f"{msg['temperature']:.1f}",
                f"{msg['humidity']:.1f}",
                msg["timestamp"]
            ))
            # keep only last 100 rows
            if len(self.table.get_children()) > 100:
                self.table.delete(self.table.get_children()[0])
        self.root.after(0, tk_add)


    def _on_summary(self, s: dict):
        def tk_update():
            # battery widgets
            batt = s.get("battery", 0.0)
            returning = s.get("returning", False)
            self.b_prog["value"] = batt
            self.lbl_batt.config(text=f"{batt:.1f} %{' 🛬' if returning else ''}")

            # buttons
            self.btn_drain.config(state="disabled" if returning else "normal")
            self.btn_charge.config(state="normal")

            # averages
            t = s.get("average_temperature")
            h = s.get("average_humidity")
            self.lbl_avg_t.config(text=f"Avg T: {t:.1f} °C" if t is not None else "Avg T: ––– °C")
            self.lbl_avg_h.config(text=f"Avg H: {h:.1f} %"  if h is not None else "Avg H: ––– %")

            # anomalies
            for an in s.get("anomalies", []):
                self.lst_anom.insert(
                    "end",
                    f"{an['timestamp']}  {an['type']}={an['value']}"
                )
                self.lst_anom.itemconfig("end", foreground="red")
        self.root.after(0, tk_update)





    def _append_log(self, txt: str):
        def tk_log():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", txt + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.root.after(0, tk_log)

    # =========================================================
    #   battery control buttons
    # =========================================================

    def _drain5(self):
        self.drone.battery.level = max(0, self.drone.battery.level - 5)

    def _recharge(self):
        self.drone.battery.charge()

    # =========================================================

    def run(self):
        self.start_background()
        self.root.mainloop()


# ───────────────────── CLI entry‑point ─────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Drone GUI wrapper")
    p.add_argument("--listen-port",  type=int, default=5000)
    p.add_argument("--central-ip",   default="127.0.0.1")
    p.add_argument("--central-port", type=int, default=6000)
    p.add_argument("--id",           default="drone-1")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-8s | %(message)s",
                        datefmt="%H:%M:%S")

    gui = DroneGUI(args)
    gui.run()
