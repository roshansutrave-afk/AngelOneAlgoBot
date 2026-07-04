"""
gui_launcher.py
Multi-tab desktop control panel. Tabs:
  1. Controls  — start/stop paper trading bot
  2. Scan      — configure and launch a full universe scan, results in
                 a sortable/filterable table with export to CSV
  3. Backtest  — configure and run a backtest
  4. Logs      — live-streamed output from all subprocesses

Tkinter only — no extra dependencies.
"""
from __future__ import annotations
import csv
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
SCAN_RESULTS_DIR = BASE_DIR / "scan_results"
SCAN_RESULTS_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess runner (shared)
# ─────────────────────────────────────────────────────────────────────────────

class ProcessRunner:
    def __init__(self, cmd: list[str], cwd: Path, output_queue: "queue.Queue[str]"):
        self._cmd = cmd
        self._cwd = cwd
        self._q = output_queue
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        if self.is_running():
            return
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            self._cmd, cwd=self._cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=flags,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.process.stdout:
            self._q.put(("LOG", line.rstrip()))
        self._q.put(("LOG", f"[process exited: {self.process.wait()}]"))
        self._q.put(("DONE", None))

    def stop(self) -> None:
        if self.is_running():
            self.process.terminate()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


# ─────────────────────────────────────────────────────────────────────────────
# Scan results watcher — polls scan_results/ for new CSV files
# ─────────────────────────────────────────────────────────────────────────────

class ScanResultsWatcher:
    """
    Watches SCAN_RESULTS_DIR for the most recently written CSV.
    Pushes ("SCAN_ROWS", list_of_dicts) into the main queue when a
    newer file appears. This decouples the GUI table from the subprocess.
    """
    def __init__(self, out_queue: "queue.Queue"):
        self._q = out_queue
        self._last_mtime: float = 0.0
        self._running = False

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            latest = self._latest_csv()
            if latest:
                mtime = latest.stat().st_mtime
                if mtime > self._last_mtime:
                    self._last_mtime = mtime
                    rows = self._read(latest)
                    if rows:
                        self._q.put(("SCAN_ROWS", rows))
            time.sleep(2)

    @staticmethod
    def _latest_csv() -> Path | None:
        csvs = sorted(SCAN_RESULTS_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        return csvs[-1] if csvs else None

    @staticmethod
    def _read(path: Path) -> list[dict]:
        try:
            with open(path, newline="") as f:
                return list(csv.DictReader(f))
        except Exception:
            return []


# ─────────────────────────────────────────────────────────────────────────────
# Main GUI
# ─────────────────────────────────────────────────────────────────────────────

class AlgoBotGUI(tk.Tk):

    SCAN_COLS = ("symbol", "exchange", "action", "confidence", "last_price",
                 "reason", "instrument_type", "expiry", "lot_size")

    def __init__(self):
        super().__init__()
        self.title("AngelOne AlgoBot — Control Panel")
        self.geometry("1060x680")
        self.minsize(900, 560)

        self._q: queue.Queue = queue.Queue()
        self._bot_runner: ProcessRunner | None = None
        self._scan_runner: ProcessRunner | None = None
        self._bt_runner: ProcessRunner | None = None
        self._watcher = ScanResultsWatcher(self._q)
        self._watcher.start()

        self._build_ui()
        self.after(200, self._drain)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._tab_controls = ttk.Frame(self._nb)
        self._tab_scan = ttk.Frame(self._nb)
        self._tab_bt = ttk.Frame(self._nb)
        self._tab_logs = ttk.Frame(self._nb)

        self._nb.add(self._tab_controls, text="  Controls  ")
        self._nb.add(self._tab_scan, text="  Market Scan  ")
        self._nb.add(self._tab_bt, text="  Backtest  ")
        self._nb.add(self._tab_logs, text="  Logs  ")

        self._build_controls_tab()
        self._build_scan_tab()
        self._build_backtest_tab()
        self._build_logs_tab()

    # ── Tab 1: Controls ──────────────────────────────────────────────────────

    def _build_controls_tab(self) -> None:
        f = self._tab_controls
        pad = {"padx": 10, "pady": 6}

        ttk.Label(f, text="Paper Trading Bot", font=("", 12, "bold")).pack(anchor="w", **pad)

        btn_row = ttk.Frame(f)
        btn_row.pack(anchor="w", padx=10)
        ttk.Button(btn_row, text="▶  Start Bot (PAPER)", width=22,
                   command=self._start_bot).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="■  Stop Bot", width=14,
                   command=self._stop_bot).pack(side="left")

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=10, pady=12)

        ttk.Label(f, text="Utilities", font=("", 12, "bold")).pack(anchor="w", **pad)
        util_row = ttk.Frame(f)
        util_row.pack(anchor="w", padx=10)
        ttk.Button(util_row, text="Open .env", command=self._open_env).pack(side="left", padx=(0, 8))
        ttk.Button(util_row, text="Open logs folder", command=self._open_logs).pack(side="left", padx=(0, 8))
        ttk.Button(util_row, text="Open equity curve", command=self._open_equity).pack(side="left", padx=(0, 8))
        ttk.Button(util_row, text="Run test suite", command=self._run_tests).pack(side="left")

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=10, pady=12)

        self._status_var = tk.StringVar(value="Status: idle")
        ttk.Label(f, textvariable=self._status_var, foreground="#2a8", font=("", 10)).pack(anchor="w", padx=10)

    # ── Tab 2: Scan ──────────────────────────────────────────────────────────

    def _build_scan_tab(self) -> None:
        f = self._tab_scan

        # Config row
        cfg = ttk.LabelFrame(f, text="Scan Configuration", padding=8)
        cfg.pack(fill="x", padx=8, pady=(8, 4))

        # Segment checkboxes
        seg_frame = ttk.Frame(cfg)
        seg_frame.grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 6))
        ttk.Label(seg_frame, text="Segments:").pack(side="left")
        self._seg_vars: dict[str, tk.BooleanVar] = {}
        for seg in ["NSE", "BSE", "NFO", "MCX", "CDS"]:
            v = tk.BooleanVar(value=(seg in {"NSE", "NFO"}))
            self._seg_vars[seg] = v
            ttk.Checkbutton(seg_frame, text=seg, variable=v).pack(side="left", padx=4)

        self._scan_interval = tk.StringVar(value="ONE_DAY")
        self._scan_lookback = tk.StringVar(value="90")
        self._scan_max_nse = tk.StringVar(value="500")
        self._scan_max_nfo = tk.StringVar(value="200")
        self._scan_max_mcx = tk.StringVar(value="50")
        self._scan_max_cds = tk.StringVar(value="30")

        def _lbl_entry(parent, label, var, row, col, width=8):
            ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(0, 2))
            ttk.Entry(parent, textvariable=var, width=width).grid(row=row, column=col + 1, padx=(0, 12))

        _lbl_entry(cfg, "Interval:", self._scan_interval, 1, 0, 14)
        _lbl_entry(cfg, "Lookback days:", self._scan_lookback, 1, 2)
        _lbl_entry(cfg, "Max NSE:", self._scan_max_nse, 1, 4)
        _lbl_entry(cfg, "Max NFO:", self._scan_max_nfo, 1, 6)

        _lbl_entry(cfg, "Max MCX:", self._scan_max_mcx, 2, 0)
        _lbl_entry(cfg, "Max CDS:", self._scan_max_cds, 2, 2)

        btn_row = ttk.Frame(cfg)
        btn_row.grid(row=3, column=0, columnspan=8, sticky="w", pady=(8, 0))
        ttk.Button(btn_row, text="▶  Run Scan", width=16, command=self._run_scan).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="■  Stop Scan", command=self._stop_scan).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Export CSV", command=self._export_scan).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Clear", command=self._clear_scan).pack(side="left")
        self._scan_status = tk.StringVar(value="")
        ttk.Label(btn_row, textvariable=self._scan_status, foreground="#888").pack(side="left", padx=12)

        # Filter bar
        filter_row = ttk.Frame(f)
        filter_row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(filter_row, text="Filter action:").pack(side="left")
        self._filter_action = tk.StringVar(value="ALL")
        for val in ["ALL", "BUY", "SELL"]:
            ttk.Radiobutton(filter_row, text=val, variable=self._filter_action,
                            value=val, command=self._apply_filter).pack(side="left", padx=4)
        ttk.Label(filter_row, text="  Symbol contains:").pack(side="left", padx=(12, 2))
        self._filter_sym = tk.StringVar()
        self._filter_sym.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(filter_row, textvariable=self._filter_sym, width=16).pack(side="left")

        # Results table
        tbl_frame = ttk.Frame(f)
        tbl_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._scan_tree = ttk.Treeview(tbl_frame, columns=self.SCAN_COLS,
                                        show="headings", selectmode="browse")
        col_widths = {"symbol": 130, "exchange": 65, "action": 55,
                      "confidence": 80, "last_price": 80, "reason": 280,
                      "instrument_type": 90, "expiry": 85, "lot_size": 65}
        for col in self.SCAN_COLS:
            self._scan_tree.heading(col, text=col.replace("_", " ").title(),
                                     command=lambda c=col: self._sort_scan(c))
            self._scan_tree.column(col, width=col_widths.get(col, 80), anchor="center")

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical", command=self._scan_tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient="horizontal", command=self._scan_tree.xview)
        self._scan_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._scan_tree.tag_configure("buy", background="#1a3a1a", foreground="#7fdd7f")
        self._scan_tree.tag_configure("sell", background="#3a1a1a", foreground="#dd7f7f")

        self._scan_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tbl_frame.columnconfigure(0, weight=1)
        tbl_frame.rowconfigure(0, weight=1)

        self._scan_rows: list[dict] = []

    # ── Tab 3: Backtest ──────────────────────────────────────────────────────

    def _build_backtest_tab(self) -> None:
        f = self._tab_bt
        form = ttk.LabelFrame(f, text="Backtest Configuration", padding=10)
        form.pack(fill="x", padx=8, pady=8)

        self._bt_symbol = tk.StringVar(value="SBIN-EQ")
        self._bt_exchange = tk.StringVar(value="NSE")
        self._bt_interval = tk.StringVar(value="ONE_DAY")
        self._bt_days = tk.StringVar(value="365")
        self._bt_capital = tk.StringVar(value="100000")
        self._bt_csv = tk.StringVar(value="")

        def _le(label, var, row, col, width=14):
            ttk.Label(form, text=label).grid(row=row, column=col, sticky="w", padx=(0, 4))
            ttk.Entry(form, textvariable=var, width=width).grid(row=row, column=col + 1, padx=(0, 12))

        _le("Symbol:", self._bt_symbol, 0, 0)
        _le("Exchange:", self._bt_exchange, 0, 2)
        _le("Interval:", self._bt_interval, 0, 4)
        _le("Days:", self._bt_days, 1, 0)
        _le("Capital:", self._bt_capital, 1, 2)

        ttk.Button(form, text="Browse CSV", command=self._browse_bt_csv).grid(row=1, column=4, padx=4)
        ttk.Entry(form, textvariable=self._bt_csv, width=28).grid(row=1, column=5, padx=4)
        ttk.Label(form, text="(CSV overrides Symbol/Exchange/Interval/Days if set)",
                  foreground="#888").grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))

        btn_row = ttk.Frame(f)
        btn_row.pack(anchor="w", padx=8, pady=(0, 8))
        ttk.Button(btn_row, text="▶  Run Backtest", command=self._run_backtest).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Open equity curve", command=self._open_equity).pack(side="left")

    # ── Tab 4: Logs ──────────────────────────────────────────────────────────

    def _build_logs_tab(self) -> None:
        f = self._tab_logs
        btn_row = ttk.Frame(f)
        btn_row.pack(fill="x", padx=8, pady=4)
        ttk.Button(btn_row, text="Clear", command=self._clear_log).pack(side="left")
        ttk.Button(btn_row, text="Save log", command=self._save_log).pack(side="left", padx=8)

        self._log_text = tk.Text(f, wrap="none", state="disabled",
                                  bg="#0d0d0d", fg="#c8c8c8", font=("Consolas", 9))
        vsb = ttk.Scrollbar(f, command=self._log_text.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=self._log_text.xview)
        self._log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self._log_text.pack(fill="both", expand=True, padx=(8, 0), pady=(0, 8))

    # ── Drain queue ──────────────────────────────────────────────────────────

    def _drain(self) -> None:
        try:
            while True:
                msg_type, payload = self._q.get_nowait()
                if msg_type == "LOG":
                    self._append_log(payload)
                elif msg_type == "DONE":
                    self._status_var.set("Status: idle")
                    self._scan_status.set("")
                elif msg_type == "SCAN_ROWS":
                    self._scan_rows = payload
                    self._apply_filter()
                    self._scan_status.set(f"{len(payload)} signals loaded")
        except queue.Empty:
            pass
        self.after(200, self._drain)

    # ── Controls actions ─────────────────────────────────────────────────────

    def _start_bot(self) -> None:
        if self._bot_runner and self._bot_runner.is_running():
            messagebox.showinfo("Running", "Bot is already running.")
            return
        if not (BASE_DIR / ".env").exists():
            messagebox.showerror("No .env", "Create .env from .env.example first.")
            return
        self._append_log(">>> Starting paper trading bot...")
        self._status_var.set("Status: bot running")
        self._bot_runner = ProcessRunner([PYTHON, "main.py"], BASE_DIR, self._q)
        self._bot_runner.start()

    def _stop_bot(self) -> None:
        if self._bot_runner and self._bot_runner.is_running():
            self._bot_runner.stop()
            self._status_var.set("Status: stopping...")
        else:
            self._append_log("Bot is not running.")

    def _run_tests(self) -> None:
        self._append_log(">>> python -m pytest tests/ -v")
        ProcessRunner([PYTHON, "-m", "pytest", "tests/", "-v"], BASE_DIR, self._q).start()

    # ── Scan actions ─────────────────────────────────────────────────────────

    def _run_scan(self) -> None:
        if self._scan_runner and self._scan_runner.is_running():
            messagebox.showinfo("Running", "A scan is already in progress.")
            return
        segs = [s for s, v in self._seg_vars.items() if v.get()]
        if not segs:
            messagebox.showerror("No segments", "Select at least one exchange segment.")
            return
        cmd = [
            PYTHON, "scan_runner.py",
            "--segments", *segs,
            "--interval", self._scan_interval.get(),
            "--lookback-days", self._scan_lookback.get(),
            "--max-nse", self._scan_max_nse.get(),
            "--max-nfo", self._scan_max_nfo.get(),
            "--max-mcx", self._scan_max_mcx.get(),
            "--max-cds", self._scan_max_cds.get(),
        ]
        self._append_log(f">>> {' '.join(cmd)}")
        self._scan_status.set("Scanning...")
        self._scan_runner = ProcessRunner(cmd, BASE_DIR, self._q)
        self._scan_runner.start()

    def _stop_scan(self) -> None:
        if self._scan_runner and self._scan_runner.is_running():
            self._scan_runner.stop()
            self._scan_status.set("Stopped")
        else:
            self._append_log("No scan running.")

    def _apply_filter(self) -> None:
        action_f = self._filter_action.get()
        sym_f = self._filter_sym.get().upper()
        for item in self._scan_tree.get_children():
            self._scan_tree.delete(item)
        for row in self._scan_rows:
            if action_f != "ALL" and row.get("action", "") != action_f:
                continue
            if sym_f and sym_f not in row.get("symbol", "").upper():
                continue
            tag = row.get("action", "").lower()
            self._scan_tree.insert("", "end",
                values=tuple(row.get(c, "") for c in self.SCAN_COLS),
                tags=(tag,))

    def _sort_scan(self, col: str) -> None:
        try:
            self._scan_rows.sort(key=lambda r: float(r.get(col, 0))
                                  if col in {"confidence", "last_price", "lot_size"}
                                  else r.get(col, ""))
        except Exception:
            pass
        self._apply_filter()

    def _clear_scan(self) -> None:
        self._scan_rows.clear()
        for item in self._scan_tree.get_children():
            self._scan_tree.delete(item)
        self._scan_status.set("")

    def _export_scan(self) -> None:
        if not self._scan_rows:
            messagebox.showinfo("Empty", "No scan results to export yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialdir=str(SCAN_RESULTS_DIR))
        if not path:
            return
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.SCAN_COLS)
            writer.writeheader()
            writer.writerows({c: r.get(c, "") for c in self.SCAN_COLS} for r in self._scan_rows)
        self._append_log(f"Exported scan results to {path}")

    # ── Backtest actions ─────────────────────────────────────────────────────

    def _run_backtest(self) -> None:
        if self._bt_runner and self._bt_runner.is_running():
            messagebox.showinfo("Running", "Backtest already in progress.")
            return
        if self._bt_csv.get():
            cmd = [PYTHON, "backtest_runner.py", "--csv", self._bt_csv.get(),
                   "--initial-capital", self._bt_capital.get()]
        else:
            cmd = [PYTHON, "backtest_runner.py",
                   "--symbol", self._bt_symbol.get(),
                   "--exchange", self._bt_exchange.get(),
                   "--interval", self._bt_interval.get(),
                   "--days", self._bt_days.get(),
                   "--initial-capital", self._bt_capital.get()]
        self._append_log(f">>> {' '.join(cmd)}")
        self._bt_runner = ProcessRunner(cmd, BASE_DIR, self._q)
        self._bt_runner.start()
        self._nb.select(self._tab_logs)  # jump to logs tab to watch output

    def _browse_bt_csv(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if p:
            self._bt_csv.set(p)

    # ── Utility actions ──────────────────────────────────────────────────────

    def _open_env(self) -> None:
        env_path = BASE_DIR / ".env"
        if not env_path.exists():
            if messagebox.askyesno("Missing", "No .env found — create from .env.example?"):
                env_path.write_text((BASE_DIR / ".env.example").read_text())
            else:
                return
        self._open_path(env_path)

    def _open_logs(self) -> None:
        (BASE_DIR / "logs").mkdir(exist_ok=True)
        self._open_path(BASE_DIR / "logs")

    def _open_equity(self) -> None:
        p = BASE_DIR / "backtest_results" / "equity_curve.png"
        if not p.exists():
            messagebox.showinfo("Not found", "Run a backtest first.")
            return
        self._open_path(p)

    @staticmethod
    def _open_path(path: Path) -> None:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.run(["xdg-open", str(path)])

    # ── Log helpers ──────────────────────────────────────────────────────────

    def _append_log(self, text: str) -> None:
        self._log_text.configure(state="normal")
        self._log_text.insert("end", text + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _save_log(self) -> None:
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if p:
            Path(p).write_text(self._log_text.get("1.0", "end"))

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def on_close(self) -> None:
        self._watcher.stop()
        for runner in (self._bot_runner, self._scan_runner, self._bt_runner):
            if runner:
                runner.stop()
        self.destroy()


if __name__ == "__main__":
    app = AlgoBotGUI()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
