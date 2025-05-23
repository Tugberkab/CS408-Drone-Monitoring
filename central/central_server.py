# central/central_server.py
"""
Central Server
~~~~~~~~~~~~~~~~~~~~~~~~~
• Async TCP server: receives newline-delimited JSON summaries from drones
• Tkinter GUI (--gui) shows:
    – rolling table of averages + battery %
    – progress-bar + drain / recharge buttons per drone
    – anomaly list
    – scrolling log
• Sends control packets back to drones over the same TCP socket
"""

from __future__ import annotations
import asyncio, json, argparse, logging
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Any, List

MAX_HISTORY = 200          # keep last N summaries in memory

class CentralServer:
    def __init__(self, listen_port: int, with_gui: bool):
        self.listen_port   = listen_port
        self.with_gui      = with_gui
        self.history: Deque[Dict[str, Any]] = deque(maxlen=MAX_HISTORY)

        # GUI widgets (created lazily)
        self._root = self._table = self._anom_list = self._log = None
        self._bars: Dict[str, Any]      = {}   # drone_id -> Progressbar
        self._controls: Dict[str, Any]  = {}   # drone_id -> Frame of buttons

        self._writers: Dict[str, asyncio.StreamWriter] = {}  # drone_id -> writer

    # ─────────── low-level TCP server ────────────
    async def _handle_drone(self,
                            reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._log_msg(f"✅ Drone connected from {peer}")

        try:
            while line := await reader.readline():
                try:
                    obj = json.loads(line)

                    # Register writer for control messages
                    did = obj.get("drone_id")
                    if did:
                        self._writers[did] = writer

                    self.history.append(obj)
                    self._log_msg(f"📥 {obj}")
                    self._update_gui(obj)
                except json.JSONDecodeError as e:
                    self._log_msg(f"⚠️ bad JSON: {e}")
        except asyncio.IncompleteReadError:
            pass
        finally:
            self._log_msg(f"❌ Drone {peer} disconnected")
            writer.close(); await writer.wait_closed()

    async def start_server(self):
        self._loop = asyncio.get_running_loop()   # remember the loop object
        srv = await asyncio.start_server(
            self._handle_drone, host="127.0.0.1", port=self.listen_port)
        addrs = ", ".join(str(s.getsockname()) for s in srv.sockets)
        self._log_msg(f"🖥️ Central listening on {addrs}")
        async with srv:
            await srv.serve_forever()

    # ─────────── Tkinter GUI helpers ─────────────
    def _build_gui(self):
        import tkinter as tk
        from tkinter import ttk, scrolledtext

        self._tk = tk  # save ref
        self._root = tk.Tk()
        self._root.title("Central Server – Phase-3")
        self._root.geometry("720x540")

        # averages table
        cols = ("timestamp", "drone_id", "battery", "avg_temp", "avg_hum")
        self._table = ttk.Treeview(self._root, columns=cols,
                                   show="headings", height=8)
        for c, w in zip(cols, (200, 80, 80, 100, 100)):
            self._table.heading(c, text=c)
            self._table.column(c, width=w, anchor="center")
        self._table.pack(fill="x", padx=6, pady=6)

        # anomaly list
        tk.Label(self._root, text="Anomalies").pack(pady=(4,0))
        self._anom_list = tk.Listbox(self._root, height=6)
        self._anom_list.pack(fill="x", padx=6)

        # log pane
        tk.Label(self._root, text="Log").pack(pady=(6,0))
        self._log = scrolledtext.ScrolledText(self._root, height=10,
                                              state="disabled")
        self._log.pack(fill="both", expand=True, padx=6, pady=(0,6))

    # progress-bar + buttons per drone
    def _ensure_controls(self, drone_id: str):
        if drone_id in self._controls:
            return
        from tkinter import ttk
        frm = ttk.Frame(self._root); frm.pack(pady=3)
        ttk.Label(frm, text=f"{drone_id} battery").pack(side="left", padx=(0,4))

        bar = ttk.Progressbar(frm, length=140, maximum=100, mode="determinate")
        bar.pack(side="left", padx=(0,6))
        self._bars[drone_id] = bar
        self._controls[drone_id] = frm
    
    def _set_controls_state(self, drone_id: str, returning: bool):
        state = "disabled" if returning else "normal"
        if drone_id in self._controls:
            for child in self._controls[drone_id].winfo_children():
                if isinstance(child, self._tk.Button):
                    child.config(state=state)


    def _send_batt(self, drone_id: str, action: str, amount: int):
        """Send a battery control message to a specific drone."""
        w = self._writers.get(drone_id)
        if not w:
            self._log_msg(f"⚠️ no socket for {drone_id}")
            return
        msg = {"type": "battery", "target": drone_id,
               "action": action, "amount": amount}
        try:
            w.write((json.dumps(msg) + "\n").encode())
            if hasattr(self, "_loop"):
                asyncio.run_coroutine_threadsafe(w.drain(), self._loop)
            self._log_msg(f"➡️ sent {action} to {drone_id}")
        except Exception as e:
            self._log_msg(f"❌ control send failed: {e}")

    # GUI marshal
    def _update_gui(self, obj: Dict[str, Any]):
        if not obj or not self.with_gui or self._root is None:
            return

        ts   = obj.get("timestamp", "—")
        did  = obj.get("drone_id",   "?")
        batt = obj.get("battery",    "—")
        ret  = obj.get("returning", False)
        temp = obj.get("average_temperature", "—")
        hum  = obj.get("average_humidity",    "—")
        anomalies = obj.get("anomalies", [])

        def _tk_update():
            self._ensure_controls(did)
            self._set_controls_state(did, ret)
            batt_disp = f"{batt}{' 🛬' if ret else ''}"
            self._bars[did]["value"] = batt if isinstance(batt, (int,float)) else 0

            self._table.insert(
                "", "end",
                values=(ts, did, batt_disp,
                        temp if not ret else "—",
                        hum  if not ret else "—")
            )

            for an in anomalies:
                self._anom_list.insert(
                    "end",
                    f"{an['timestamp']} | {did} | {an['type']}={an['value']}"
                )
                self._anom_list.itemconfig("end", foreground="red")
        self._root.after(0, _tk_update)




    # unified logger (console + GUI)
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

    # ─────────── Tk mainloop wrapper ─────────────
    def _gui_mainloop(self):
        import threading
        threading.Thread(target=lambda: asyncio.run(self.start_server()),
                         daemon=True).start()
        self._root.mainloop()


# ─────────── entry-point ────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Central Server")
    parser.add_argument("--listen-port", type=int, default=6000)
    parser.add_argument("--gui", action="store_true",
                        help="Launch Tk GUI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(message)s")

    central = CentralServer(args.listen_port, with_gui=args.gui)

    if args.gui:
        central._build_gui()
        central._gui_mainloop()      # blocks
    else:
        asyncio.run(central.start_server())
