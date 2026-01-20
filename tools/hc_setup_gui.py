#!/usr/bin/env python3
"""
Tkinter GUI for HC-05 / HC-06 setup, reusing hc_core logic.

Fixes added:
- Pair/Setup tabs are scrollable (Canvas + Scrollbar)
- Mouse wheel scroll works even when cursor is over ttk widgets (combobox/entry/checkbutton)
- Plan Preview + Log have both vertical/horizontal scrollbars and wheel support
- Thread-safe dialogs (no Tk calls in worker thread)
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Callable, Dict, List, Optional, Tuple

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in os.sys.path:
    os.sys.path.insert(0, THIS_DIR)

import serial  # pyserial
from serial import SerialException

from hc_core import (  # noqa: E402
    HC06_BAUD_MAP,
    PairFlags,
    detect_module,
    format_port_entry,
    list_serial_ports,
    parse_addr_response,
    run_pair,
    send_command,
)

BAUD_CHOICES = [
    "9600",
    "19200",
    "38400",
    "57600",
    "115200",
    "230400",
    "460800",
    "921600",
]


class SetupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("HC-05 / HC-06 Setup Wizard")
        self.root.geometry("820x640")

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_event: Optional[threading.Event] = None
        self.port_map: Dict[str, str] = {}
        self.last_detected_module: Optional[str] = None

        # scroll targets (set later)
        self._setup_canvas: Optional[tk.Canvas] = None
        self._setup_body: Optional[ttk.Frame] = None
        self._pair_canvas: Optional[tk.Canvas] = None
        self._pair_body: Optional[ttk.Frame] = None

        self._build_ui()
        self._bind_global_mousewheel()

        self.refresh_ports()
        self._update_mode_state()
        self._update_single_plan_preview()
        self._poll_log_queue()

        # Default: open Pair tab
        self.notebook.select(self.pair_tab)

    # -------------------------
    # Thread-safe UI helpers
    # -------------------------
    def _ui_sync(self, fn: Callable[[], object]) -> object:
        if threading.current_thread() is threading.main_thread():
            return fn()

        done = threading.Event()
        out: Dict[str, object] = {"val": None, "err": None}

        def _run():
            try:
                out["val"] = fn()
            except Exception as e:
                out["err"] = e
            finally:
                done.set()

        self.root.after(0, _run)
        done.wait()
        if out["err"] is not None:
            raise out["err"]  # type: ignore[misc]
        return out["val"]

    def _show_info(self, title: str, msg: str) -> None:
        self._ui_sync(lambda: messagebox.showinfo(title, msg, parent=self.root))

    def _show_warn(self, title: str, msg: str) -> None:
        self._ui_sync(lambda: messagebox.showwarning(title, msg, parent=self.root))

    def _show_error(self, title: str, msg: str) -> None:
        self._ui_sync(lambda: messagebox.showerror(title, msg, parent=self.root))

    def _ask_string(self, title: str, prompt: str) -> Optional[str]:
        return self._ui_sync(lambda: simpledialog.askstring(title, prompt, parent=self.root))  # type: ignore[return-value]

    # -------------------------
    # Scrollable tab helper
    # -------------------------
    def _make_scrollable_tab(self, parent: ttk.Frame) -> Tuple[tk.Canvas, ttk.Frame]:
        """
        Create a scrollable area (canvas + vertical scrollbar) inside a tab.
        Returns (canvas, inner_frame).
        """
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(_e):
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)

        def _on_canvas_configure(e):
            # keep inner width same as visible canvas width
            canvas.itemconfigure(win_id, width=e.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        return canvas, inner

    def _is_descendant(self, w: Optional[tk.Misc], ancestor: Optional[tk.Misc]) -> bool:
        if w is None or ancestor is None:
            return False
        cur = w
        while cur is not None:
            if cur == ancestor:
                return True
            cur = getattr(cur, "master", None)
        return False

    def _wheel_units(self, event) -> int:
        # Windows/macOS use delta, Linux uses Button-4/5
        if getattr(event, "delta", 0):
            return int(-1 * (event.delta / 120))
        if getattr(event, "num", None) == 4:
            return -3
        if getattr(event, "num", None) == 5:
            return 3
        return 0

    def _bind_global_mousewheel(self) -> None:
        # Bind once for whole app, then decide where to scroll based on cursor position.
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_global_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_global_mousewheel, add="+")

    def _on_global_mousewheel(self, event):
        units = self._wheel_units(event)
        if units == 0:
            return

        # what widget is under cursor?
        w = self.root.winfo_containing(event.x_root, event.y_root)

        # Priority 1: plan/log text (so text scroll feels natural)
        if hasattr(self, "plan_text") and self._is_descendant(w, self.plan_text):
            self.plan_text.yview_scroll(units, "units")
            return "break"

        if hasattr(self, "log_text") and self._is_descendant(w, self.log_text):
            self.log_text.yview_scroll(units, "units")
            return "break"

        # Priority 2: scrollable tabs
        if self._pair_body is not None and self._pair_canvas is not None and self._is_descendant(w, self._pair_body):
            self._pair_canvas.yview_scroll(units, "units")
            return "break"

        if self._setup_body is not None and self._setup_canvas is not None and self._is_descendant(w, self._setup_body):
            self._setup_canvas.yview_scroll(units, "units")
            return "break"

        return

    # -------------------------
    # UI build
    # -------------------------
    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True)

        self.setup_tab = ttk.Frame(self.notebook)
        self.pair_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.setup_tab, text="Setup single")
        self.notebook.add(self.pair_tab, text="Pair master/slave")

        # Make both tabs scrollable
        self._setup_canvas, setup_body = self._make_scrollable_tab(self.setup_tab)
        self._setup_body = setup_body

        self._pair_canvas, pair_body = self._make_scrollable_tab(self.pair_tab)
        self._pair_body = pair_body

        # =========================
        # Setup single tab (build into setup_body)
        # =========================
        top = ttk.Frame(setup_body)
        top.pack(fill="x", pady=4)

        ttk.Label(top, text="Serial Port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, state="readonly", width=50)
        self.port_combo.pack(side="left", padx=5, fill="x", expand=True)
        ttk.Button(top, text="Refresh", command=self.refresh_ports).pack(side="left", padx=5)

        ttk.Label(top, text="Module:").pack(side="left", padx=(10, 2))
        self.module_var = tk.StringVar(value="auto")
        self.module_combo = ttk.Combobox(
            top, textvariable=self.module_var, state="readonly", values=["auto", "hc05", "hc06"], width=8
        )
        self.module_combo.pack(side="left")
        self.module_combo.bind("<<ComboboxSelected>>", lambda _: self._on_single_inputs_changed())

        row2 = ttk.Frame(setup_body)
        row2.pack(fill="x", pady=4)

        ttk.Label(row2, text="Name:").pack(side="left")
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(row2, textvariable=self.name_var, width=18)
        self.name_entry.pack(side="left", padx=5)

        ttk.Label(row2, text="PIN (4 digits):").pack(side="left", padx=(10, 2))
        self.pin_var = tk.StringVar()
        self.pin_entry = ttk.Entry(row2, textvariable=self.pin_var, width=10)
        self.pin_entry.pack(side="left")

        ttk.Label(row2, text="Baud:").pack(side="left", padx=(10, 2))
        self.baud_var = tk.StringVar(value=BAUD_CHOICES[4])  # default 115200
        self.baud_combo = ttk.Combobox(row2, textvariable=self.baud_var, values=BAUD_CHOICES, width=12, state="readonly")
        self.baud_combo.pack(side="left")

        ttk.Label(row2, text="Role (HC-05):").pack(side="left", padx=(10, 2))
        self.role_var = tk.StringVar(value="slave")
        self.role_combo = ttk.Combobox(
            row2, textvariable=self.role_var, state="disabled", values=["slave", "master"], width=8
        )
        self.role_combo.pack(side="left")

        sug = ttk.Frame(setup_body)
        sug.pack(fill="x", pady=(6, 2))
        ttk.Label(sug, text="Quick presets:").pack(side="left")
        ttk.Button(sug, text="Suggest SLAVE", command=self._suggest_slave_single).pack(side="left", padx=6)
        ttk.Button(sug, text="Suggest MASTER", command=self._suggest_master_single).pack(side="left")

        steps_box = ttk.Labelframe(setup_body, text="Single Setup - Step toggles", padding=8)
        steps_box.pack(fill="x", pady=6)

        self.step_set_name = tk.BooleanVar(value=True)
        self.step_set_pin = tk.BooleanVar(value=True)
        self.step_set_uart = tk.BooleanVar(value=True)
        self.step_set_role = tk.BooleanVar(value=True)
        self.step_read_addr = tk.BooleanVar(value=False)
        self.step_reset = tk.BooleanVar(value=True)

        ttk.Checkbutton(steps_box, text="Set NAME", variable=self.step_set_name, command=self._on_single_inputs_changed).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(steps_box, text="Set PIN/PSWD", variable=self.step_set_pin, command=self._on_single_inputs_changed).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(steps_box, text="Set UART/BAUD", variable=self.step_set_uart, command=self._on_single_inputs_changed).grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Checkbutton(steps_box, text="Set ROLE (HC-05)", variable=self.step_set_role, command=self._on_single_inputs_changed).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(steps_box, text="Read ADDR? (HC-05)", variable=self.step_read_addr, command=self._on_single_inputs_changed).grid(row=1, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(steps_box, text="RESET (HC-05)", variable=self.step_reset, command=self._on_single_inputs_changed).grid(row=1, column=2, sticky="w", padx=(12, 0))

        for c in range(3):
            steps_box.columnconfigure(c, weight=1)

        plan_box = ttk.Labelframe(setup_body, text="Plan Preview (commands will run in this order)", padding=8)
        plan_box.pack(fill="x", pady=6)

        plan_inner = ttk.Frame(plan_box)
        plan_inner.pack(fill="both", expand=True)

        self.plan_text = tk.Text(plan_inner, height=9, wrap="none", state="disabled")
        plan_vsb = ttk.Scrollbar(plan_inner, orient="vertical", command=self.plan_text.yview)
        plan_hsb = ttk.Scrollbar(plan_inner, orient="horizontal", command=self.plan_text.xview)
        self.plan_text.configure(yscrollcommand=plan_vsb.set, xscrollcommand=plan_hsb.set)

        self.plan_text.grid(row=0, column=0, sticky="nsew")
        plan_vsb.grid(row=0, column=1, sticky="ns")
        plan_hsb.grid(row=1, column=0, sticky="ew")
        plan_inner.rowconfigure(0, weight=1)
        plan_inner.columnconfigure(0, weight=1)

        btn_row = ttk.Frame(setup_body)
        btn_row.pack(fill="x", pady=6)

        self.detect_btn = ttk.Button(btn_row, text="Detect", command=self.on_detect)
        self.detect_btn.pack(side="left")

        self.run_btn = ttk.Button(btn_row, text="Run Single Setup", command=self.on_run_single)
        self.run_btn.pack(side="left", padx=6)

        self.stop_btn = ttk.Button(btn_row, text="Stop/Cancel", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left")

        for var in (self.name_var, self.pin_var, self.baud_var, self.role_var, self.module_var):
            var.trace_add("write", lambda *_: self._on_single_inputs_changed())

        # =========================
        # Pair tab (build into pair_body)
        # =========================
        mode_row = ttk.Frame(pair_body)
        mode_row.pack(fill="x", pady=4)
        ttk.Label(mode_row, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value="two")
        self.mode_combo = ttk.Combobox(mode_row, textvariable=self.mode_var, state="readonly", values=["two", "one"], width=10)
        self.mode_combo.pack(side="left", padx=4)
        self.mode_combo.bind("<<ComboboxSelected>>", lambda _: self._update_mode_state())
        ttk.Button(mode_row, text="Refresh Ports", command=self.refresh_ports).pack(side="left", padx=6)

        prow1 = ttk.Frame(pair_body)
        prow1.pack(fill="x", pady=4)
        self.master_label = ttk.Label(prow1, text="MASTER Port:")
        self.master_label.pack(side="left")
        self.master_port_var = tk.StringVar()
        self.master_port_combo = ttk.Combobox(prow1, textvariable=self.master_port_var, state="readonly", width=35)
        self.master_port_combo.pack(side="left", padx=4)
        self.slave_label = ttk.Label(prow1, text="SLAVE Port:")
        self.slave_label.pack(side="left")
        self.slave_port_var = tk.StringVar()
        self.slave_port_combo = ttk.Combobox(prow1, textvariable=self.slave_port_var, state="readonly", width=35)
        self.slave_port_combo.pack(side="left", padx=4)

        prow2 = ttk.Frame(pair_body)
        prow2.pack(fill="x", pady=4)
        ttk.Label(prow2, text="Name MASTER:").pack(side="left")
        self.name_master_var = tk.StringVar()
        ttk.Entry(prow2, textvariable=self.name_master_var, width=15).pack(side="left", padx=4)
        ttk.Label(prow2, text="Name SLAVE:").pack(side="left")
        self.name_slave_var = tk.StringVar()
        ttk.Entry(prow2, textvariable=self.name_slave_var, width=15).pack(side="left", padx=4)

        ttk.Label(prow2, text="PIN:").pack(side="left", padx=(10, 2))
        self.pin_pair_var = tk.StringVar(value="1234")
        ttk.Entry(prow2, textvariable=self.pin_pair_var, width=10).pack(side="left")

        ttk.Label(prow2, text="Baud:").pack(side="left", padx=(10, 2))
        self.baud_pair_var = tk.StringVar(value=BAUD_CHOICES[4])  # default 115200
        ttk.Combobox(prow2, textvariable=self.baud_pair_var, values=BAUD_CHOICES, width=12, state="readonly").pack(side="left")

        ttk.Label(
            pair_body,
            text="Mode one: swap 1 adapter. NOTE: LINK may fail if SLAVE is unpowered after swap; MASTER will still BIND.",
        ).pack(anchor="w")

        adv_frame = ttk.Labelframe(pair_body, text="Advanced commands", padding=6)
        adv_frame.pack(fill="x", pady=8)

        adv_opts = ttk.Frame(adv_frame)
        adv_opts.pack(fill="x", pady=2)
        self.advanced_var = tk.BooleanVar(value=False)
        self.basic_var = tk.BooleanVar(value=True)
        self.dry_run_var = tk.BooleanVar(value=False)
        self.no_orlg_var = tk.BooleanVar(value=False)
        self.no_rmaad_var = tk.BooleanVar(value=False)

        self.advanced_check = ttk.Checkbutton(adv_opts, text="Advanced", variable=self.advanced_var, command=self._update_advanced_state)
        self.advanced_check.pack(side="left")
        self.basic_check = ttk.Checkbutton(adv_opts, text="Run basic", variable=self.basic_var, command=self._update_advanced_state)
        self.basic_check.pack(side="left", padx=4)
        self.dry_run_check = ttk.Checkbutton(adv_opts, text="Dry run (plan only)", variable=self.dry_run_var)
        self.dry_run_check.pack(side="left", padx=4)
        self.no_orlg_check = ttk.Checkbutton(adv_opts, text="Skip ORGL", variable=self.no_orlg_var)
        self.no_orlg_check.pack(side="left", padx=4)
        self.no_rmaad_check = ttk.Checkbutton(adv_opts, text="Skip RMAAD", variable=self.no_rmaad_var)
        self.no_rmaad_check.pack(side="left", padx=4)

        steps_frame = ttk.Frame(adv_frame)
        steps_frame.pack(fill="x", pady=4)
        ttk.Label(steps_frame, text="SLAVE basic steps:").grid(row=0, column=0, sticky="w")
        ttk.Label(steps_frame, text="MASTER basic steps:").grid(row=0, column=1, sticky="w", padx=(12, 0))

        self.slave_step_vars: Dict[str, tk.BooleanVar] = {}
        self.slave_step_widgets: Dict[str, ttk.Checkbutton] = {}
        self.slave_critical = {"at", "role0", "uart"}

        slave_steps = [
            ("at", "AT (critical)"),
            ("orlg", "AT+ORGL"),
            ("role0", "AT+ROLE=0 (critical)"),
            ("name", "AT+NAME"),
            ("pin", "AT+PSWD/PIN"),
            ("uart", "UART/BAUD (critical)"),
            ("addr", "AT+ADDR? (mode=one required)"),
        ]
        for idx, (sid, label) in enumerate(slave_steps, start=1):
            var = tk.BooleanVar(value=True)
            chk = ttk.Checkbutton(steps_frame, text=label, variable=var)
            chk.grid(row=idx, column=0, sticky="w", pady=1)
            self.slave_step_vars[sid] = var
            self.slave_step_widgets[sid] = chk

        self.master_step_vars: Dict[str, tk.BooleanVar] = {}
        self.master_step_widgets: Dict[str, ttk.Checkbutton] = {}
        self.master_critical = {"at", "role1", "cmode", "uart", "bind", "link"}

        master_steps = [
            ("at", "AT (critical)"),
            ("role1", "AT+ROLE=1 (critical)"),
            ("cmode", "AT+CMODE=0 (critical)"),
            ("name", "AT+NAME"),
            ("pin", "AT+PSWD/PIN"),
            ("uart", "UART/BAUD (critical)"),
            ("rmaad", "AT+RMAAD"),
            ("init", "AT+INIT"),
            ("pair", "AT+PAIR (optional)"),
            ("bind", "AT+BIND (critical)"),
            ("link", "AT+LINK (optional in mode one)"),
            ("reset", "AT+RESET"),
        ]
        for idx, (sid, label) in enumerate(master_steps, start=1):
            var = tk.BooleanVar(value=True)
            chk = ttk.Checkbutton(steps_frame, text=label, variable=var)
            chk.grid(row=idx, column=1, sticky="w", padx=(12, 0), pady=1)
            self.master_step_vars[sid] = var
            self.master_step_widgets[sid] = chk

        extra_frame = ttk.Frame(adv_frame)
        extra_frame.pack(fill="x", pady=4)
        extra_frame.columnconfigure(0, weight=1)
        extra_frame.columnconfigure(1, weight=1)
        ttk.Label(extra_frame, text="Extra SLAVE commands (one per line):").grid(row=0, column=0, sticky="w")
        ttk.Label(extra_frame, text="Extra MASTER commands (one per line):").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.extra_slave_text = tk.Text(extra_frame, height=4, width=45, state="disabled", wrap="none")
        self.extra_slave_text.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=2)
        self.extra_master_text = tk.Text(extra_frame, height=4, width=45, state="disabled", wrap="none")
        self.extra_master_text.grid(row=1, column=1, sticky="nsew", pady=2)

        pbtn_row = ttk.Frame(pair_body)
        pbtn_row.pack(fill="x", pady=10)
        self.pair_detect_btn = ttk.Button(pbtn_row, text="Detect", command=self.on_pair_detect)
        self.pair_detect_btn.pack(side="left")
        self.pair_run_btn = ttk.Button(pbtn_row, text="Pair Now", command=self.on_pair_run)
        self.pair_run_btn.pack(side="left", padx=6)
        self.pair_stop_btn = ttk.Button(pbtn_row, text="Stop/Cancel", command=self.on_stop, state="disabled")
        self.pair_stop_btn.pack(side="left")

        # =========================
        # Shared status + log (outside scroll tabs)
        # =========================
        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(main, textvariable=self.status_var, foreground="blue")
        self.status_label.pack(anchor="w", pady=(6, 4))

        log_frame = ttk.Frame(main)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, height=14, wrap="none", state="disabled", bg="#f6f6f6")
        vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        hsb = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.log_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

    # -------------------------
    # Ports
    # -------------------------
    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        display_list: List[str] = []
        self.port_map.clear()

        for p in ports:
            display = format_port_entry(p)
            self.port_map[display] = p.device
            display_list.append(display)

        self.port_combo["values"] = display_list
        self.master_port_combo["values"] = display_list
        self.slave_port_combo["values"] = display_list

        if display_list:
            if not self.port_var.get().strip():
                self.port_combo.current(0)
            if not self.master_port_var.get().strip():
                self.master_port_combo.current(0)
            if not self.slave_port_var.get().strip():
                self.slave_port_combo.current(1 if len(display_list) > 1 else 0)
            self._set_status(f"Found {len(display_list)} port(s).", "blue")
        else:
            self.port_var.set("")
            self.master_port_var.set("")
            self.slave_port_var.set("")
            self._set_status("No serial ports found. Plug in USB-TTL and refresh.", "red")
            self._show_info(
                "No serial ports",
                "No serial ports found.\n\n"
                "- Plug USB-TTL\n"
                "- Install drivers (CH340/CP210x)\n"
                "- Check permissions / COM port access\n",
            )

        self._update_mode_state()
        self._update_single_plan_preview()

    # -------------------------
    # Presets
    # -------------------------
    def _suggest_slave_single(self) -> None:
        self.module_var.set("hc05")
        self.role_var.set("slave")
        if not self.name_var.get().strip():
            self.name_var.set("SLAVE")
        if not self.pin_var.get().strip():
            self.pin_var.set("1234")
        self.step_read_addr.set(True)
        self._on_single_inputs_changed()

    def _suggest_master_single(self) -> None:
        self.module_var.set("hc05")
        self.role_var.set("master")
        if not self.name_var.get().strip():
            self.name_var.set("MASTER")
        if not self.pin_var.get().strip():
            self.pin_var.set("1234")
        self.step_read_addr.set(False)
        self._on_single_inputs_changed()

    def _on_single_inputs_changed(self) -> None:
        self._update_role_state()
        self._update_single_plan_preview()

    # -------------------------
    # Plan preview
    # -------------------------
    def _infer_single_module_for_plan(self) -> Optional[str]:
        sel = self.module_var.get().strip().lower()
        if sel in ("hc05", "hc06"):
            return sel
        return self.last_detected_module

    def _build_single_plan_lines(self, module: str) -> List[str]:
        name = self.name_var.get().strip()
        pin = self.pin_var.get().strip()
        baud = self.baud_var.get().strip()
        role = self.role_var.get().strip().lower()

        want_name = self.step_set_name.get()
        want_pin = self.step_set_pin.get()
        want_uart = self.step_set_uart.get()
        want_role = self.step_set_role.get()
        want_addr = self.step_read_addr.get()
        want_reset = self.step_reset.get()

        lines: List[str] = []
        lines.append(f"MODULE: {module.upper()}")
        lines.append("1) AT  (critical)")
        idx = 2

        if module == "hc05":
            if want_name:
                lines.append(f"{idx}) AT+NAME={name if name else '<MISSING NAME> !!'}")
                idx += 1
            if want_pin:
                lines.append(f"{idx}) AT+PSWD={pin if pin else '<MISSING PIN> !!'} (fallback AT+PIN=xxxx)")
                idx += 1
            if want_uart:
                lines.append(f"{idx}) AT+UART={baud if baud else '<MISSING BAUD> !!'},0,0")
                idx += 1
            if want_role:
                rv = "0" if role != "master" else "1"
                lines.append(f"{idx}) AT+ROLE={rv} ({role})")
                idx += 1
            if want_addr:
                lines.append(f"{idx}) AT+ADDR? (read address)")
                idx += 1
            if want_reset:
                lines.append(f"{idx}) AT+RESET (optional)")
                idx += 1
        else:
            if want_name:
                lines.append(f"{idx}) AT+NAME{name if name else '<MISSING NAME> !!'} (fallback AT+NAME=<name>)")
                idx += 1
            if want_pin:
                lines.append(f"{idx}) AT+PIN{pin if pin else '<MISSING PIN> !!'} (fallback AT+PSWD=xxxx)")
                idx += 1
            if want_uart:
                if baud:
                    try:
                        b = int(baud)
                        code = HC06_BAUD_MAP.get(b)
                    except Exception:
                        code = None
                    lines.append(f"{idx}) AT+BAUD{code if code else '?'} (baud={baud})")
                else:
                    lines.append(f"{idx}) AT+BAUD<MISSING BAUD> !!")
                idx += 1
            if want_addr:
                lines.append(f"{idx}) AT+ADDR? (often unsupported on HC-06)")
                idx += 1
            if want_reset:
                lines.append(f"{idx}) (skip) RESET not standard on HC-06")
                idx += 1

        return lines

    def _update_single_plan_preview(self) -> None:
        sel = self.module_var.get().strip().lower()
        inferred = self._infer_single_module_for_plan()

        text_lines: List[str] = ["=== SINGLE SETUP PLAN ==="]
        if sel == "auto" and inferred is None:
            text_lines.append("Module = AUTO (unknown yet). Run Detect to get exact plan.")
            text_lines.append("")
            text_lines.extend(self._build_single_plan_lines("hc05"))
            text_lines.append("")
            text_lines.extend(self._build_single_plan_lines("hc06"))
        else:
            module = inferred or sel
            text_lines.append(f"(Module selection: {sel.upper()} / inferred: {(inferred or 'N/A').upper()})")
            text_lines.append("")
            text_lines.extend(self._build_single_plan_lines(module))

        self.plan_text.configure(state="normal")
        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("end", "\n".join(text_lines) + "\n")
        self.plan_text.configure(state="disabled")

    # -------------------------
    # Buttons
    # -------------------------
    def on_detect(self) -> None:
        params = self._collect_single_params(validate_only_port=True)
        if not params:
            return
        self._start_worker(mode="detect", params=params)

    def on_run_single(self) -> None:
        params = self._collect_single_params(validate_only_port=False)
        if not params:
            return
        self._start_worker(mode="single-setup", params=params)

    def on_pair_detect(self) -> None:
        params = self._collect_pair_params()
        if not params:
            return
        self._start_worker(mode="pair-detect", params=params)

    def on_pair_run(self) -> None:
        params = self._collect_pair_params()
        if not params:
            return
        self._start_worker(mode="pair-run", params=params)

    def on_stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()
            self._append_log("[CANCEL] Requested stop.")
            self._set_status("Cancelling...", "orange")

    # -------------------------
    # Collect params
    # -------------------------
    def _collect_single_params(self, *, validate_only_port: bool) -> Optional[dict]:
        display_port = self.port_var.get().strip()
        if not display_port:
            self._show_error("Missing port", "Please select a serial port.")
            return None
        port = self.port_map.get(display_port, display_port)

        module = self.module_var.get().strip().lower()
        if module not in ("auto", "hc05", "hc06"):
            module = "auto"

        steps = {
            "set_name": bool(self.step_set_name.get()),
            "set_pin": bool(self.step_set_pin.get()),
            "set_uart": bool(self.step_set_uart.get()),
            "set_role": bool(self.step_set_role.get()),
            "read_addr": bool(self.step_read_addr.get()),
            "reset": bool(self.step_reset.get()),
        }

        name = self.name_var.get().strip() or None
        pin = self.pin_var.get().strip() or None
        role = self.role_var.get().strip().lower()
        baud_str = self.baud_var.get().strip()

        if validate_only_port:
            return {"port": port, "module": module, "name": name, "pin": pin, "baud": baud_str, "role": role, "steps": steps}

        if steps["set_name"] and not name:
            ans = self._ask_string("Missing NAME", "You enabled 'Set NAME' but Name is empty.\nEnter NAME:")
            if not ans:
                return None
            ans = ans.strip()
            if not ans:
                return None
            self.name_var.set(ans)
            name = ans

        if steps["set_pin"] and not pin:
            ans = self._ask_string("Missing PIN", "You enabled 'Set PIN/PSWD' but PIN is empty.\nEnter 4-digit PIN:")
            if not ans:
                return None
            ans = ans.strip()
            if not ans:
                return None
            self.pin_var.set(ans)
            pin = ans

        if pin and (not pin.isdigit() or len(pin) != 4):
            self._show_error("PIN invalid", "PIN must be exactly 4 digits.")
            return None

        try:
            baud = int(baud_str)
            if baud <= 0:
                raise ValueError
        except ValueError:
            self._show_error("Baud invalid", "Baud must be a positive integer.")
            return None

        if role not in ("slave", "master"):
            role = "slave"

        return {"port": port, "module": module, "name": name, "pin": pin, "baud": baud, "role": role, "steps": steps}

    def _collect_pair_params(self) -> Optional[dict]:
        mode = self.mode_var.get().strip().lower()
        mp_display = self.master_port_var.get().strip()
        sp_display = self.slave_port_var.get().strip()

        if mode == "one":
            shared_display = mp_display or sp_display
            if not shared_display:
                self._show_error("Missing port", "Select one port for mode ONE.")
                return None
            shared_port = self.port_map.get(shared_display, shared_display)
            master_port = shared_port
            slave_port = shared_port
        else:
            if not mp_display or not sp_display:
                self._show_error("Missing port", "Select MASTER and SLAVE ports for mode TWO.")
                return None
            master_port = self.port_map.get(mp_display, mp_display)
            slave_port = self.port_map.get(sp_display, sp_display)
            if master_port == slave_port:
                self._show_error("Ports duplicate", "MASTER and SLAVE must differ.")
                return None

        pin = self.pin_pair_var.get().strip() or None
        if pin and (not pin.isdigit() or len(pin) != 4):
            self._show_error("PIN invalid", "PIN must be exactly 4 digits or blank.")
            return None
        try:
            baud = int(self.baud_pair_var.get().strip())
            if baud <= 0:
                raise ValueError
        except ValueError:
            self._show_error("Baud invalid", "Baud must be a positive integer.")
            return None

        advanced_mode = self.advanced_var.get()
        flags = PairFlags()
        flags.basic = self.basic_var.get()
        flags.advanced = advanced_mode
        flags.interactive = False
        flags.dry_run = self.dry_run_var.get()
        flags.no_orlg = self.no_orlg_var.get()
        flags.no_rmaad = self.no_rmaad_var.get()
        flags.show_plan = advanced_mode or flags.dry_run

        if advanced_mode and flags.basic:
            skip_steps = set()
            for sid, var in self.slave_step_vars.items():
                if not var.get() and sid not in self.slave_critical:
                    if sid == "addr" and mode == "one":
                        self.slave_step_vars[sid].set(True)
                        continue
                    skip_steps.add(sid)
            for sid, var in self.master_step_vars.items():
                if not var.get() and sid not in self.master_critical:
                    skip_steps.add(sid)
            flags.skip_steps = skip_steps

        flags.extra_slave_cmds = self._collect_extra_commands(self.extra_slave_text) if advanced_mode else []
        flags.extra_master_cmds = self._collect_extra_commands(self.extra_master_text) if advanced_mode else []

        return {
            "mode": mode,
            "port": master_port,
            "master_port": master_port,
            "slave_port": slave_port,
            "name_master": self.name_master_var.get().strip() or None,
            "name_slave": self.name_slave_var.get().strip() or None,
            "pin": pin,
            "baud": baud,
            "flags": flags,
        }

    def _collect_extra_commands(self, widget: tk.Text) -> List[str]:
        content = widget.get("1.0", "end")
        return [line.strip() for line in content.splitlines() if line.strip()]

    # -------------------------
    # Worker control
    # -------------------------
    def _set_controls_running(self, running: bool) -> None:
        if running:
            self.detect_btn.configure(state="disabled")
            self.run_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.pair_detect_btn.configure(state="disabled")
            self.pair_run_btn.configure(state="disabled")
            self.pair_stop_btn.configure(state="normal")
        else:
            self.detect_btn.configure(state="normal")
            self.run_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.pair_detect_btn.configure(state="normal")
            self.pair_run_btn.configure(state="normal")
            self.pair_stop_btn.configure(state="disabled")

    def _start_worker(self, mode: str, params: dict) -> None:
        if self.worker and self.worker.is_alive():
            self._show_warn("Running", "A task is already running. Stop/Cancel first.")
            return

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.stop_event = threading.Event()
        self._set_controls_running(True)
        self._set_status("Running...", "blue")

        def worker():
            success = False
            try:
                if mode == "detect":
                    success = self._do_detect(params)
                elif mode == "single-setup":
                    success = self._do_single_setup(params)
                elif mode == "pair-detect":
                    success = self._do_pair_detect(params)
                elif mode == "pair-run":
                    success = self._do_pair_run(params)
            except Exception as exc:
                self._append_log(f"[EXCEPTION] {exc!r}")
                success = False
            finally:
                self.root.after(0, lambda: self._finish_worker(success))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _finish_worker(self, success: bool) -> None:
        self._set_controls_running(False)
        self.worker = None
        self.stop_event = None
        if not success and self.status_var.get() == "Running...":
            self._set_status("Failed", "red")

    # -------------------------
    # Actions
    # -------------------------
    def _do_detect(self, params: dict) -> bool:
        res = detect_module(params["port"], logger=self._append_log, stop_event=self.stop_event)
        if not res:
            self._append_log("[FAIL] Detect failed.")
            self._set_status("Detect failed", "red")
            self._show_error("Detect failed", "Could not detect module.\n\nCheck wiring and AT mode.")
            return False

        self.last_detected_module = res.module
        self._append_log(f"[DETECT] {res.module.upper()} using {res.profile.baud} / {res.profile.line_ending}")
        if res.role_response.strip():
            self._append_log(f"[DETECT] ROLE? {res.role_response.strip()}")

        self._set_status(f"Detected {res.module.upper()}", "green")
        self._ui_sync(lambda: self._on_single_inputs_changed())
        return True

    def _do_single_setup(self, params: dict) -> bool:
        port: str = params["port"]
        module_sel: str = params["module"]
        name: Optional[str] = params["name"]
        pin: Optional[str] = params["pin"]
        baud: int = params["baud"]
        role: str = params["role"]
        steps = params["steps"]

        det = detect_module(port, logger=self._append_log, stop_event=self.stop_event)
        if not det:
            self._append_log("[FAIL] Detect failed. Cannot run setup.")
            self._set_status("Setup failed", "red")
            self._show_error("Setup failed", "Detect failed. Check wiring and AT mode.")
            return False

        detected = det.module
        use_module = detected if module_sel == "auto" else module_sel
        if use_module != detected:
            self._append_log(f"[WARN] Forced module={use_module}, but detection suggested {detected}.")

        profile = det.profile
        self._append_log(f"[SETUP] Using profile {profile.baud}/{profile.line_ending} on {port} ({use_module.upper()})")

        try:
            with serial.Serial(port, baudrate=profile.baud, timeout=0.8, write_timeout=1) as ser:
                ok, _ = send_command(ser, "AT", profile, expect_ok=True, retries=2, logger=self._append_log, stop_event=self.stop_event)
                if not ok:
                    raise RuntimeError("AT failed")

                if use_module == "hc05":
                    if steps["set_name"] and name:
                        ok, _ = send_command(ser, f"AT+NAME={name}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            raise RuntimeError("NAME failed")

                    if steps["set_pin"] and pin:
                        ok, _ = send_command(ser, f"AT+PSWD={pin}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            self._append_log(".. AT+PSWD failed; trying AT+PIN=xxxx")
                            ok, _ = send_command(ser, f"AT+PIN={pin}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            raise RuntimeError("PIN/PSWD failed")

                    if steps["set_uart"]:
                        ok, _ = send_command(ser, f"AT+UART={baud},0,0", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            raise RuntimeError("UART failed")

                    if steps["set_role"]:
                        rv = 0 if role != "master" else 1
                        ok, _ = send_command(ser, f"AT+ROLE={rv}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            raise RuntimeError("ROLE failed")

                    if steps["read_addr"]:
                        _, resp = send_command(ser, "AT+ADDR?", profile, expect_ok=False, logger=self._append_log, stop_event=self.stop_event)
                        parsed = parse_addr_response(resp or "")
                        if parsed:
                            self._append_log(f"[ADDR] {parsed[0]}  (use: {parsed[1]})")
                        else:
                            self._append_log("[ADDR] (could not parse)")

                    if steps["reset"]:
                        send_command(ser, "AT+RESET", profile, expect_ok=False, logger=self._append_log, stop_event=self.stop_event)

                else:
                    if steps["set_name"] and name:
                        ok, _ = send_command(ser, f"AT+NAME{name}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            self._append_log(".. NAME without '=' failed; trying AT+NAME=<name>")
                            ok, _ = send_command(ser, f"AT+NAME={name}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            raise RuntimeError("NAME failed")

                    if steps["set_pin"] and pin:
                        ok, _ = send_command(ser, f"AT+PIN{pin}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            self._append_log(".. PINxxxx failed; trying AT+PSWD=xxxx")
                            ok, _ = send_command(ser, f"AT+PSWD={pin}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            raise RuntimeError("PIN failed")

                    if steps["set_uart"]:
                        code = HC06_BAUD_MAP.get(int(baud))
                        if not code:
                            raise RuntimeError(f"Baud {baud} not in HC-06 BAUD map.")
                        ok, _ = send_command(ser, f"AT+BAUD{code}", profile, expect_ok=True, logger=self._append_log, stop_event=self.stop_event)
                        if not ok:
                            raise RuntimeError("BAUD failed")

                    if steps["read_addr"]:
                        _, resp = send_command(ser, "AT+ADDR?", profile, expect_ok=False, logger=self._append_log, stop_event=self.stop_event)
                        parsed = parse_addr_response(resp or "")
                        if parsed:
                            self._append_log(f"[ADDR] {parsed[0]}  (use: {parsed[1]})")
                        else:
                            self._append_log("[ADDR] (likely unsupported on this HC-06)")

            if self.stop_event and self.stop_event.is_set():
                self._set_status("Cancelled", "orange")
                self._show_warn("Cancelled", "Task stopped.")
                return False

            self._set_status("Single Setup OK", "green")
            self._show_info("Success", "Single setup complete.\nSee log for details.")
            return True

        except (SerialException, RuntimeError) as exc:
            if self.stop_event and self.stop_event.is_set():
                self._set_status("Cancelled", "orange")
                self._show_warn("Cancelled", "Task stopped.")
                return False
            self._append_log(f"[FAIL] {exc}")
            self._set_status("Single Setup failed", "red")
            self._show_error("Single Setup failed", "Single setup failed.\nSee log for details.")
            return False

    def _do_pair_detect(self, params: dict) -> bool:
        success = True
        ports: List[Tuple[str, str]] = []
        mode = params["mode"]

        if mode == "one":
            ports.append(("SHARED", params["master_port"]))
        else:
            ports.append(("MASTER", params["master_port"]))
            ports.append(("SLAVE", params["slave_port"]))

        for label, port in ports:
            if self.stop_event and self.stop_event.is_set():
                break
            res = detect_module(port, logger=lambda m, lbl=label: self._append_log(f"[{lbl}] {m}"), stop_event=self.stop_event)
            if not res:
                self._append_log(f"[{label}] Detect failed.")
                success = False
            else:
                self._append_log(f"[{label}] Detected {res.module.upper()} using {res.profile.baud} / {res.profile.line_ending}")

        self._set_status("Detect OK" if success else "Detect issues", "green" if success else "orange")
        return success

    def _do_pair_run(self, params: dict) -> bool:
        def prompt_swap(msg: str, default_port: str) -> str:
            ans = self._ask_string("Swap to MASTER", f"{msg}\nMASTER port (Enter to keep {default_port}):")
            ans = (ans or "").strip()
            return ans or default_port

        def choose_addr_cb(addrs):
            if not addrs:
                return None
            if len(addrs) == 1:
                return addrs[0]
            options = "\n".join(f"[{idx+1}] {a[0]} (use {a[1]})" for idx, a in enumerate(addrs))
            ans = self._ask_string("Select INQ address", f"Found:\n{options}\n\nEnter number (1-{len(addrs)}) or Cancel:")
            if not ans:
                return None
            ans = ans.strip()
            if ans.isdigit():
                i = int(ans)
                if 1 <= i <= len(addrs):
                    return addrs[i - 1]
            return None

        ok = run_pair(
            mode=params["mode"],
            master_port=params["master_port"],
            slave_port=params["slave_port"],
            port=params["port"],
            name_master=params["name_master"],
            name_slave=params["name_slave"],
            pin=params["pin"],
            baud=params["baud"],
            flags=params["flags"],
            prompt_swap=prompt_swap,
            choose_addr_cb=choose_addr_cb,
            logger=self._append_log,
            stop_event=self.stop_event,
        )

        if ok:
            self._set_status("Pair OK", "green")
            if params["mode"] == "one":
                self._show_info(
                    "Success",
                    "Pair/setup done.\n\n"
                    "If mode ONE: MASTER may be configured (BIND) even if LINK failed.\n"
                    "NEXT: Power both modules in DATA mode (KEY/EN LOW). MASTER should auto-connect.",
                )
            else:
                self._show_info("Success", "Pair/setup done.\nSee log for details.")
        else:
            if self.stop_event and self.stop_event.is_set():
                self._set_status("Cancelled", "orange")
                self._show_warn("Cancelled", "Task stopped.")
            else:
                self._set_status("Pair failed", "red")
                self._show_error("Pair failed", "Pairing failed.\nSee log for details.")
        return ok

    # -------------------------
    # Logging + status
    # -------------------------
    def _append_log(self, line: str) -> None:
        self.log_queue.put(line)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.configure(foreground=color)

    # -------------------------
    # Pair UI state
    # -------------------------
    def _update_mode_state(self, *_args) -> None:
        mode = self.mode_var.get().lower()
        if mode == "one":
            self.master_label.configure(text="Shared Port:")
            self.master_port_combo.state(["!disabled"])
            self.slave_label.configure(text="SLAVE Port:")
            self.slave_port_combo.state(["disabled"])
        else:
            self.master_label.configure(text="MASTER Port:")
            self.slave_label.configure(text="SLAVE Port:")
            self.master_port_combo.state(["!disabled"])
            self.slave_port_combo.state(["!disabled"])
        self._update_advanced_state()

    def _update_advanced_state(self, *_args) -> None:
        adv = self.advanced_var.get()
        basic = self.basic_var.get()
        mode = self.mode_var.get().lower()

        for widget in (self.basic_check, self.dry_run_check, self.no_orlg_check, self.no_rmaad_check):
            widget.state(["!disabled"] if adv else ["disabled"])

        text_state = "normal" if adv else "disabled"
        self.extra_slave_text.configure(state=text_state)
        self.extra_master_text.configure(state=text_state)

        if not adv:
            for var in self.slave_step_vars.values():
                var.set(True)
            for var in self.master_step_vars.values():
                var.set(True)

        for sid, chk in self.slave_step_widgets.items():
            enable = adv and basic
            if sid in self.slave_critical or (sid == "addr" and mode == "one"):
                self.slave_step_vars[sid].set(True)
                enable = False
            chk.state(["!disabled"] if enable else ["disabled"])

        for sid, chk in self.master_step_widgets.items():
            enable = adv and basic
            if sid in self.master_critical:
                self.master_step_vars[sid].set(True)
                enable = False
            chk.state(["!disabled"] if enable else ["disabled"])

    # -------------------------
    # Single role enable/disable
    # -------------------------
    def _update_role_state(self) -> None:
        sel = self.module_var.get().strip().lower()
        inferred = self.last_detected_module
        effective = sel if sel != "auto" else (inferred or "auto")

        if effective == "hc05":
            self.role_combo.configure(state="readonly")
        else:
            self.role_combo.configure(state="disabled")
            self.step_set_role.set(False)


def main() -> None:
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
