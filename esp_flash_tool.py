# python -m pip install esptool pyserial
# pyinstaller --onefile --noconsole --collect-all esptool esp_flash_tool.py

import os
import sys
import threading
import queue
import io
import runpy
import contextlib
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

# Requires: pyserial
import serial
from serial.tools import list_ports


CHIPS = ["auto", "esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c2", "esp32c6", "esp32h2"]
BAUDS = ["115200", "460800", "921600"]


def find_flash_args(path: Path) -> Path | None:
    """
    Accept either:
      - project root (contains build/flash_args)
      - build folder (contains flash_args)
    """
    path = path.resolve()
    candidates = [
        path / "flash_args",           # build dir
        path / "build" / "flash_args"  # project root
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def list_serial_ports():
    """Return list of (display, device) tuples."""
    ports = []
    for p in list_ports.comports():
        desc = (p.description or "").strip()
        hwid = (p.hwid or "").strip()
        display = f"{p.device} — {desc}"
        if "VID:PID" in hwid:
            display += f" ({hwid})"
        ports.append((display, p.device))

    def com_sort_key(item):
        dev = item[1].upper()
        if dev.startswith("COM"):
            try:
                return int(dev[3:])
            except ValueError:
                return 9999
        return 9999

    ports.sort(key=com_sort_key)
    return ports


class LineBufferedQueueWriter(io.TextIOBase):
    """
    File-like object that buffers writes and emits complete lines to a queue.
    This keeps GUI output clean (no tiny partial chunks).
    """
    def __init__(self, log_q: queue.Queue, kind: str):
        super().__init__()
        self.log_q = log_q
        self.kind = kind
        self._buf = ""

    def writable(self):
        return True

    def write(self, s):
        if not s:
            return 0

        # Treat carriage return as a "line boundary" to show progress updates cleanly.
        s = s.replace("\r", "\n")

        self._buf += s

        # Emit complete lines only
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.log_q.put((self.kind, line + "\n"))

        return len(s)

    def flush(self):
        # Emit whatever remains (last partial line)
        if self._buf:
            self.log_q.put((self.kind, self._buf))
            self._buf = ""


def run_esptool_in_process(argv, cwd: Path, log_q: queue.Queue):
    """
    Runs esptool as if executing: python -m esptool ...
    Streams stdout/stderr live into log_q (line-buffered for clean output).
    """
    old_argv = sys.argv[:]
    old_cwd = os.getcwd()

    qout = LineBufferedQueueWriter(log_q, "OUT")
    qerr = LineBufferedQueueWriter(log_q, "ERR")

    try:
        os.chdir(str(cwd))
        sys.argv = argv

        with contextlib.redirect_stdout(qout), contextlib.redirect_stderr(qerr):
            runpy.run_module("esptool", run_name="__main__")

    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
        if code != 0:
            log_q.put(("ERR", f"\n[esptool exited with code {code}]\n"))

    except Exception as e:
        log_q.put(("ERR", f"\n[Exception] {e}\n"))

    finally:
        try:
            qout.flush()
            qerr.flush()
        except Exception:
            pass

        sys.argv = old_argv
        os.chdir(old_cwd)
        log_q.put(("DONE", ""))


class EspFlashGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP Flash GUI (Tkinter + esptool + flash_args)")
        self.geometry("860x540")
        self.minsize(820, 500)

        self.log_q = queue.Queue()
        self.worker = None

        # Serial monitor state
        self.mon_thread = None
        self.mon_stop_evt = threading.Event()
        self.mon_ser = None
        self.mon_running = False
        self.var_mon_baud = tk.StringVar(value="115200")  # ESP-IDF default
        self.var_mon_timestamp = tk.BooleanVar(value=False)
        self.mon_line_buf = ""  # buffer incoming chunks into full lines for timestamp mode

        # Variables
        self.var_path = tk.StringVar(value=str(Path.cwd()))
        self.var_port_disp = tk.StringVar(value="")
        self.var_chip = tk.StringVar(value="esp32")
        self.var_baud = tk.StringVar(value="460800")  # flash baud
        self.var_status = tk.StringVar(value="Ready")

        self.port_map = {}  # display -> device

        self._build_ui()
        self.refresh_ports()
        self.after(120, self._poll_logs)

        # Ensure we stop monitor on close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        try:
            if self.mon_running:
                self.stop_monitor()
        except Exception:
            pass
        self.destroy()

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # --- Folder selection
        row1 = ttk.Frame(root)
        row1.pack(fill="x", pady=(0, 8))
        ttk.Label(row1, text="Project root or build folder:").pack(side="left")
        ttk.Entry(row1, textvariable=self.var_path).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row1, text="Browse…", command=self.browse_folder).pack(side="left")

        # --- Port / Chip / Baud
        row2 = ttk.Frame(root)
        row2.pack(fill="x", pady=(0, 8))

        ttk.Label(row2, text="Port:").pack(side="left")
        self.cmb_port = ttk.Combobox(row2, textvariable=self.var_port_disp, state="readonly", width=52)
        self.cmb_port.pack(side="left", padx=6)
        ttk.Button(row2, text="Refresh", command=self.refresh_ports).pack(side="left", padx=(0, 12))

        ttk.Label(row2, text="Chip:").pack(side="left")
        self.cmb_chip = ttk.Combobox(row2, textvariable=self.var_chip, values=CHIPS, state="readonly", width=10)
        self.cmb_chip.pack(side="left", padx=6)

        ttk.Label(row2, text="Baud:").pack(side="left")
        self.cmb_baud = ttk.Combobox(row2, textvariable=self.var_baud, values=BAUDS, state="readonly", width=10)
        self.cmb_baud.pack(side="left", padx=6)

        # --- Buttons
        row3 = ttk.Frame(root)
        row3.pack(fill="x", pady=(0, 8))

        self.btn_flash = ttk.Button(row3, text="Flash", command=self.flash)
        self.btn_flash.pack(side="left")

        self.btn_erase = ttk.Button(row3, text="Erase Flash", command=self.erase)
        self.btn_erase.pack(side="left", padx=8)

        self.btn_mac = ttk.Button(row3, text="Read MAC", command=self.read_mac)
        self.btn_mac.pack(side="left", padx=8)

        ttk.Button(row3, text="Clear Log", command=self.clear_log).pack(side="left", padx=8)

        # --- Serial Monitor (same output view)
        self.btn_monitor = ttk.Button(row3, text="Serial Monitor", command=self.toggle_monitor)
        self.btn_monitor.pack(side="left", padx=8)

        ttk.Label(row3, text="Monitor baud:").pack(side="left", padx=(12, 0))
        self.cmb_mon_baud = ttk.Combobox(
            row3,
            textvariable=self.var_mon_baud,
            values=["115200", "74880", "230400", "460800", "921600", "1500000"],
            state="readonly",
            width=10
        )
        self.cmb_mon_baud.pack(side="left", padx=6)

        # Timestamp switch
        ttk.Checkbutton(row3, text="Timestamp", variable=self.var_mon_timestamp).pack(side="left", padx=(10, 0))

        ttk.Label(row3, textvariable=self.var_status, foreground="#444").pack(side="right")

        # --- Log window
        log_frame = ttk.LabelFrame(root, text="Output")
        log_frame.pack(fill="both", expand=True)

        self.txt = tk.Text(log_frame, wrap="word")
        self.txt.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(log_frame, command=self.txt.yview)
        sb.pack(side="right", fill="y")
        self.txt.configure(yscrollcommand=sb.set)

        self._log("Ready.\n1) Build in container (idf.py build)\n2) Select COM port and click Flash.\n")

    def _log(self, msg: str):
        self.txt.insert("end", msg)
        self.txt.see("end")

    def clear_log(self):
        self.txt.delete("1.0", "end")

    def browse_folder(self):
        path = filedialog.askdirectory(initialdir=self.var_path.get() or str(Path.cwd()))
        if path:
            self.var_path.set(path)

    def refresh_ports(self):
        ports = list_serial_ports()
        self.port_map = {disp: dev for disp, dev in ports}
        self.cmb_port["values"] = [disp for disp, _ in ports]

        if ports:
            cur = self.var_port_disp.get()
            if cur not in self.port_map:
                self.var_port_disp.set(ports[0][0])
            self.var_status.set(f"Found {len(ports)} port(s)")
        else:
            self.var_port_disp.set("")
            self.var_status.set("No ports found")

    def _selected_port(self) -> str:
        return self.port_map.get(self.var_port_disp.get(), "")

    def _disable_ui(self, disabled: bool):
        state = "disabled" if disabled else "normal"
        self.btn_flash.config(state=state)
        self.btn_erase.config(state=state)
        self.btn_mac.config(state=state)

        self.cmb_port.config(state="disabled" if disabled else ("disabled" if self.mon_running else "readonly"))
        self.cmb_chip.config(state="disabled" if disabled else ("disabled" if self.mon_running else "readonly"))
        self.cmb_baud.config(state="disabled" if disabled else ("disabled" if self.mon_running else "readonly"))

        # monitor baud cannot change while monitoring
        self.cmb_mon_baud.config(state="disabled" if (disabled or self.mon_running) else "readonly")

    def _start_task(self, argv, cwd: Path):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Another operation is running.")
            return

        # COM port can't be shared: stop monitor before esptool uses it
        if self.mon_running:
            self._log("\n[Stopping Serial Monitor to run esptool...]\n")
            self.stop_monitor()

        self._disable_ui(True)
        self.var_status.set("Working…")

        def worker():
            run_esptool_in_process(argv, cwd, self.log_q)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _resolve_flash_args_and_cwd(self) -> tuple[Path, Path]:
        base = Path(self.var_path.get()).expanduser()
        flash_args = find_flash_args(base)
        if not flash_args:
            raise FileNotFoundError(
                "flash_args not found.\n\n"
                "Select either:\n"
                " - project root (contains build/flash_args), or\n"
                " - build folder (contains flash_args)\n\n"
                "Also ensure you ran `idf.py build` inside the container."
            )
        # Run in build folder so relative bin paths in flash_args resolve
        return flash_args, flash_args.parent

    def flash(self):
        port = self._selected_port()
        if not port:
            messagebox.showerror("No Port", "Please select a COM port.")
            return

        try:
            flash_args, cwd = self._resolve_flash_args_and_cwd()
        except Exception as e:
            messagebox.showerror("Missing flash_args", str(e))
            return

        chip = self.var_chip.get().strip() or "esp32"
        baud = int(self.var_baud.get()) if self.var_baud.get().strip().isdigit() else None

        argv = ["esptool", "--chip", chip, "--port", port]
        if baud:
            argv += ["--baud", str(baud)]
        argv += ["write-flash", f"@{flash_args.name}"]

        self._log(f"\n=== FLASH: {port} | chip={chip} | baud={baud or 'default'} ===\n")
        self._log(f"Using: {flash_args}\n")
        self._start_task(argv, cwd)

    def erase(self):
        port = self._selected_port()
        if not port:
            messagebox.showerror("No Port", "Please select a COM port.")
            return

        chip = self.var_chip.get().strip() or "esp32"
        baud = int(self.var_baud.get()) if self.var_baud.get().strip().isdigit() else None

        argv = ["esptool", "--chip", chip, "--port", port]
        if baud:
            argv += ["--baud", str(baud)]
        argv += ["erase-flash"]

        self._log(f"\n=== ERASE FLASH: {port} | chip={chip} ===\n")
        self._start_task(argv, Path.cwd())

    def read_mac(self):
        port = self._selected_port()
        if not port:
            messagebox.showerror("No Port", "Please select a COM port.")
            return

        chip = self.var_chip.get().strip() or "esp32"
        argv = ["esptool", "--chip", chip, "--port", port, "read-mac"]

        self._log(f"\n=== READ MAC: {port} | chip={chip} ===\n")
        self._start_task(argv, Path.cwd())

    # ---------------- Serial Monitor (same Output view) ----------------

    def toggle_monitor(self):
        if self.mon_running:
            self.stop_monitor()
        else:
            self.start_monitor()

    def _ts_prefix(self) -> str:
        # Example: [2026-01-30 16:16:16.555]
        now = datetime.now()
        return f"[{now.strftime('%Y-%m-%d %H:%M:%S')}.{int(now.microsecond/1000):03d}] "

    def start_monitor(self):
        port = self._selected_port()
        if not port:
            messagebox.showerror("No Port", "Please select a COM port.")
            return

        # If esptool is running, don't start monitor
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Wait for the current esptool operation to finish.")
            return

        # Already running?
        if self.mon_thread and self.mon_thread.is_alive():
            return

        try:
            baud = int(self.var_mon_baud.get())
        except ValueError:
            baud = 115200
            self.var_mon_baud.set("115200")

        self.mon_stop_evt.clear()
        self.mon_line_buf = ""

        def reader():
            try:
                self.mon_ser = serial.Serial(port=port, baudrate=baud, timeout=0.2)
                self.log_q.put(("OUT", f"\n=== SERIAL MONITOR STARTED: {port} @ {baud} ===\n"))

                while not self.mon_stop_evt.is_set():
                    try:
                        n = self.mon_ser.in_waiting
                        data = self.mon_ser.read(n if n else 1)
                        if not data:
                            continue

                        txt = data.decode(errors="replace")

                        # --- IMPORTANT NEWLINE FIX ---
                        # First normalize CRLF safely:
                        txt = txt.replace("\r\n", "\n")

                        if not self.var_mon_timestamp.get():
                            # Non-timestamp mode:
                            # Do NOT convert lone '\r' to '\n' (that would double-space CRLF logs)
                            # Also remove any remaining '\r' so Tk doesn't show weird ^M behavior
                            txt = txt.replace("\r", "")
                            self.log_q.put(("OUT", txt))
                        else:
                            # Timestamp mode:
                            # Now treat any remaining lone '\r' as newline (optional, helpful for progress updates)
                            txt = txt.replace("\r", "\n")

                            self.mon_line_buf += txt
                            while "\n" in self.mon_line_buf:
                                line, self.mon_line_buf = self.mon_line_buf.split("\n", 1)

                                # Option A: keep genuine blank lines, but skip accidental trailing blank
                                if line == "" and self.mon_line_buf == "":
                                    continue

                                if line == "":
                                    # Genuine blank line (e.g. \n\n)
                                    self.log_q.put(("OUT", "\n"))
                                else:
                                    self.log_q.put(("OUT", self._ts_prefix() + line + "\n"))

                    except serial.SerialException as e:
                        self.log_q.put(("ERR", f"\n[SerialException] {e}\n"))
                        break
                    except Exception as e:
                        self.log_q.put(("ERR", f"\n[Monitor Exception] {e}\n"))
                        break

            except Exception as e:
                self.log_q.put(("ERR", f"\n[Failed to open serial port] {e}\n"))

            finally:
                # Flush remaining buffer only in timestamp mode, without forcing an extra newline
                try:
                    if self.var_mon_timestamp.get() and self.mon_line_buf:
                        self.log_q.put(("OUT", self._ts_prefix() + self.mon_line_buf))
                        self.mon_line_buf = ""
                except Exception:
                    pass

                try:
                    if self.mon_ser and self.mon_ser.is_open:
                        self.mon_ser.close()
                except Exception:
                    pass
                self.mon_ser = None
                self.log_q.put(("OUT", "\n=== SERIAL MONITOR STOPPED ===\n"))
                self.log_q.put(("MON_DONE", ""))

        self.mon_thread = threading.Thread(target=reader, daemon=True)
        self.mon_thread.start()

        self.mon_running = True
        self.btn_monitor.config(text="Stop Monitor")
        self.var_status.set("Monitoring…")

        # Disable port/chip/flash controls while monitoring (avoid port conflict)
        self.cmb_port.config(state="disabled")
        self.cmb_chip.config(state="disabled")
        self.cmb_baud.config(state="disabled")
        self.cmb_mon_baud.config(state="disabled")

        self.btn_flash.config(state="disabled")
        self.btn_erase.config(state="disabled")
        self.btn_mac.config(state="disabled")

    def stop_monitor(self):
        self.mon_stop_evt.set()
        try:
            if self.mon_ser and self.mon_ser.is_open:
                self.mon_ser.close()
        except Exception:
            pass

        self.mon_running = False
        self.btn_monitor.config(text="Serial Monitor")
        self.var_status.set("Ready")

        self.cmb_port.config(state="readonly")
        self.cmb_chip.config(state="readonly")
        self.cmb_baud.config(state="readonly")
        self.cmb_mon_baud.config(state="readonly")

        self.btn_flash.config(state="normal")
        self.btn_erase.config(state="normal")
        self.btn_mac.config(state="normal")

    # ---------------- Log polling ----------------

    def _poll_logs(self):
        """
        Batch queue messages to reduce GUI lag.
        """
        out_chunks = []
        done = False
        mon_done = False

        try:
            while True:
                kind, text = self.log_q.get_nowait()
                if kind in ("OUT", "ERR"):
                    out_chunks.append(text)
                elif kind == "DONE":
                    done = True
                elif kind == "MON_DONE":
                    mon_done = True

        except queue.Empty:
            pass

        if out_chunks:
            self._log("".join(out_chunks))

        # esptool finished
        if done:
            self._disable_ui(False)
            if self.mon_running:
                # Keep monitor-related disabling
                self.cmb_port.config(state="disabled")
                self.cmb_chip.config(state="disabled")
                self.cmb_baud.config(state="disabled")
                self.cmb_mon_baud.config(state="disabled")
                self.btn_flash.config(state="disabled")
                self.btn_erase.config(state="disabled")
                self.btn_mac.config(state="disabled")
                self.var_status.set("Monitoring…")
            else:
                self.var_status.set("Done")

        # monitor thread finished (stop or error)
        if mon_done:
            self.mon_running = False
            try:
                self.btn_monitor.config(text="Serial Monitor")
                self.cmb_mon_baud.config(state="readonly")
                self.cmb_port.config(state="readonly")
                self.cmb_chip.config(state="readonly")
                self.cmb_baud.config(state="readonly")
                self.btn_flash.config(state="normal")
                self.btn_erase.config(state="normal")
                self.btn_mac.config(state="normal")
                self.var_status.set("Ready")
            except Exception:
                pass

        self.after(120, self._poll_logs)


if __name__ == "__main__":
    try:
        app = EspFlashGUI()
        app.mainloop()
    except ModuleNotFoundError as e:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Missing dependency",
                f"{e}\n\nThis GUI requires:\n  - pyserial\n  - esptool\n\n"
                "Install them in your Python environment and re-run."
            )
        except Exception:
            print(
                f"{e}\n\nThis GUI requires:\n  - pyserial\n  - esptool\n\n"
                "Install them in your Python environment and re-run."
            )