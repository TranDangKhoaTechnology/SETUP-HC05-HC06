#!/usr/bin/env python3
"""
HC-05 / HC-06 auto-setup wizard (CLI) + auto pair master/slave.

Behavior:
- No args -> interactive menu (default = PAIR master/slave).
- Still supports legacy: `--port ...` without subcommand runs single setup.
- Subcommand: `pair` for master/slave pairing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from hc_core import (
    PairFlags,
    describe_profile,
    format_port_entry,
    list_serial_ports,
    run_pair,
    run_setup,
    detect_module,
)


# -------------------------
# Helpers: ports / prompts
# -------------------------

def print_port_menu(port_infos: List) -> None:
    for idx, p in enumerate(port_infos, start=1):
        print(f"[{idx}] {format_port_entry(p)}")


def pick_port_interactive(title: str, exclude: Optional[set[str]] = None) -> Optional[str]:
    exclude = exclude or set()
    ports = [p for p in list_serial_ports() if p.device not in exclude]
    if not ports:
        print(f"!! No serial ports available for {title}.")
        return None

    if len(ports) == 1:
        only = ports[0]
        prompt = f"Use port {format_port_entry(only)} for {title}? (Y/n) "
        choice = input(prompt).strip().lower()
        if choice in ("", "y", "yes"):
            return only.device
        print("Cancelled.")
        return None

    print(f"Select serial port for {title}:")
    print_port_menu(ports)
    attempts = 0
    while attempts < 3:
        choice = input(f"Choose port (1-{len(ports)}) or Enter to cancel: ").strip()
        if choice == "":
            print("Cancelled by user request.")
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(ports):
                return ports[idx - 1].device
        attempts += 1
        print("Invalid choice, try again.")

    print("Too many invalid attempts.")
    return None


def prompt_input(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None and default != "" else ""
    return input(f"{prompt}{suffix}: ").strip() or (default or "")


def prompt_yes_no(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    ans = input(f"{prompt} ({d}): ").strip().lower()
    if ans == "":
        return default_yes
    return ans in ("y", "yes")


def _parse_skip_steps(skip_csv: Optional[str]) -> set:
    if not skip_csv:
        return set()
    return {token.strip().lower() for token in skip_csv.split(",") if token.strip()}


# -------------------------
# Pair profile save/load
# -------------------------

def _flags_to_dict(flags: PairFlags) -> dict:
    return {
        "basic": flags.basic,
        "skip_steps": sorted(flags.skip_steps),
        "extra_master_cmds": list(flags.extra_master_cmds),
        "extra_slave_cmds": list(flags.extra_slave_cmds),
        "no_orlg": flags.no_orlg,
        "no_rmaad": flags.no_rmaad,
        "dry_run": flags.dry_run,
        "advanced": flags.advanced,
        "interactive": flags.interactive,
        "show_plan": flags.show_plan,
    }


def _flags_from_data(data: dict) -> PairFlags:
    # Backward/forward compatible: allow either flat flags or {"slave":..., "master":...}
    if "slave" in data or "master" in data:
        slave_data = data.get("slave", {}) or {}
        master_data = data.get("master", {}) or {}
        return PairFlags(
            basic=slave_data.get("basic", True),
            skip_steps=set(slave_data.get("skip_steps", [])) | set(master_data.get("skip_steps", [])),
            extra_master_cmds=master_data.get("extra_master_cmds", []) or slave_data.get("extra_master_cmds", []),
            extra_slave_cmds=slave_data.get("extra_slave_cmds", []),
            no_orlg=slave_data.get("no_orlg", False),
            no_rmaad=master_data.get("no_rmaad", False),
            dry_run=bool(slave_data.get("dry_run", False) or master_data.get("dry_run", False)),
            advanced=bool(slave_data.get("advanced", False) or master_data.get("advanced", False)),
            interactive=bool(slave_data.get("interactive", False) or master_data.get("interactive", False)),
            show_plan=bool(slave_data.get("show_plan", False) or master_data.get("show_plan", False)),
        )

    return PairFlags(
        basic=data.get("basic", True),
        skip_steps=set(data.get("skip_steps", [])),
        extra_master_cmds=data.get("extra_master_cmds", []),
        extra_slave_cmds=data.get("extra_slave_cmds", []),
        no_orlg=data.get("no_orlg", False),
        no_rmaad=data.get("no_rmaad", False),
        dry_run=data.get("dry_run", False),
        advanced=data.get("advanced", False),
        interactive=data.get("interactive", False),
        show_plan=data.get("show_plan", False),
    )


def _load_profile_file(path: Path) -> Optional[PairFlags]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"!! Profile file not found: {path}")
        return None
    except json.JSONDecodeError as exc:
        print(f"!! Could not parse profile {path}: {exc}")
        return None
    except Exception as exc:
        print(f"!! Error reading profile {path}: {exc}")
        return None
    return _flags_from_data(raw)


def _save_profile_file(path: Path, slave_flags: PairFlags, master_flags: PairFlags) -> None:
    payload = {"slave": _flags_to_dict(slave_flags), "master": _flags_to_dict(master_flags)}
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Saved profile to {path}")
    except Exception as exc:
        print(f"!! Could not save profile to {path}: {exc}")


# -------------------------
# Interactive wizards
# -------------------------

def wizard_setup_fill(args: argparse.Namespace) -> Optional[argparse.Namespace]:
    print("\n=== SINGLE SETUP wizard ===")
    port = pick_port_interactive("setup")
    if not port:
        return None
    args.port = port

    # Module
    module_default = args.module or "auto"
    while True:
        module = prompt_input("Module type (auto/hc05/hc06)", module_default).lower()
        if module in ("auto", "hc05", "hc06"):
            args.module = module
            break
        print("Enter auto, hc05, or hc06.")

    # Preset role suggestion for HC-05 (or auto)
    # User asked for "2 nút gợi ý" -> CLI version = preset pick.
    if args.module in ("hc05", "auto"):
        print("\nPreset (HC-05 only):")
        print("  [1] Suggest SLAVE  (ROLE=0)  (recommended for most cases)")
        print("  [2] Suggest MASTER (ROLE=1)  (for pairing/binding)")
        print("  [3] Custom")
        preset = prompt_input("Choose preset", "1").strip()
        if preset == "2":
            args.role = "master"
        elif preset == "3":
            # will ask role below
            pass
        else:
            args.role = "slave"

    # Name
    args.name = prompt_input("Device name (optional)", args.name or "")

    # PIN
    while True:
        pin = prompt_input("PIN (4 digits, optional)", args.pin or "")
        if not pin:
            args.pin = None
            break
        if pin.isdigit() and len(pin) == 4:
            args.pin = pin
            break
        print("PIN must be exactly 4 digits or blank to skip.")

    # Baud
    while True:
        baud_in = prompt_input("Data-mode baud", str(args.baud or 9600))
        try:
            args.baud = int(baud_in)
            if args.baud > 0:
                break
        except ValueError:
            pass
        print("Enter a positive integer baud rate.")

    # Role (only if hc05 or auto)
    if args.module in ("hc05", "auto"):
        role_default = args.role or "slave"
        while True:
            role = prompt_input("HC-05 role (slave/master)", role_default).lower()
            if role in ("slave", "master"):
                args.role = role
                break
            print("Enter slave or master.")

    return args


def wizard_pair_fill(args: argparse.Namespace) -> Optional[argparse.Namespace]:
    print("\n=== PAIR MASTER/SLAVE wizard ===")

    # Mode
    while True:
        mode = prompt_input("Mode (two=2 ports, one=swap 1 port)", (args.mode or "two")).lower()
        if mode in ("one", "two"):
            args.mode = mode
            break
        print("Enter one or two.")

    if args.mode == "one":
        shared = args.port or args.master_port or args.slave_port
        if not shared:
            shared = pick_port_interactive("pair (mode=one shared port)")
            if not shared:
                return None
        args.port = shared
        args.master_port = shared
        args.slave_port = shared
    else:
        mp = args.master_port
        sp = args.slave_port
        if not mp:
            mp = pick_port_interactive("pair MASTER")
            if not mp:
                return None
        if not sp:
            sp = pick_port_interactive("pair SLAVE", exclude={mp})
            if not sp:
                return None
        if mp == sp:
            print("!! MASTER and SLAVE ports must differ in mode=two.")
            return None
        args.master_port = mp
        args.slave_port = sp

    # Names
    args.name_master = prompt_input("Name MASTER (optional)", args.name_master or "")
    args.name_slave = prompt_input("Name SLAVE (optional)", args.name_slave or "")

    # PIN (required for pair; default 1234)
    while True:
        pin = prompt_input("PIN (4 digits)", args.pin or "1234")
        if pin.isdigit() and len(pin) == 4:
            args.pin = pin
            break
        print("PIN must be exactly 4 digits.")

    # Baud
    while True:
        baud_in = prompt_input("Data-mode baud", str(args.baud or 9600))
        try:
            baud = int(baud_in)
            if baud > 0:
                args.baud = baud
                break
        except ValueError:
            pass
        print("Enter a positive integer baud rate.")

    # Advanced (optional)
    adv = prompt_yes_no("Enable advanced interactive step selection?", default_yes=False)
    args.advanced = adv
    if adv:
        args.basic = True
        args.dry_run = False
        args.no_orig = False
        args.no_rmaad = False

        args.no_orig = prompt_yes_no("Skip ORGL on SLAVE?", default_yes=False)
        args.no_rmaad = prompt_yes_no("Skip RMAAD on MASTER?", default_yes=False)
        args.dry_run = prompt_yes_no("Dry-run (plan only, no serial writes)?", default_yes=False)

    return args


# -------------------------
# Setup plan printing
# -------------------------

def _print_setup_checklist(module: str, name: Optional[str], pin: Optional[str], baud: int, role: str) -> None:
    print("\n=== SINGLE SETUP checklist ===")
    print(f"- Module: {module}")
    print(f"- Name  : {name or '(skip)'}")
    print(f"- PIN   : {pin or '(skip)'}")
    print(f"- Baud  : {baud}")
    if module in ("hc05", "auto"):
        print(f"- Role  : {role} (HC-05 only)")
    print("\nCommands (approx):")
    print("  Detect: AT (+ROLE? to infer HC-05 vs HC-06)")
    if module in ("hc05", "auto"):
        if name:
            print(f"  - AT+NAME={name}")
        if pin:
            print(f"  - AT+PSWD={pin}  (fallback AT+PIN={pin} if needed)")
        print(f"  - AT+UART={baud},0,0")
        print(f"  - AT+ROLE={'1' if role=='master' else '0'}")
        print("  - AT+RESET (optional, some firmwares silent)")
    else:
        if name:
            print(f"  - AT+NAME{name}  (fallback AT+NAME={name})")
        if pin:
            print(f"  - AT+PIN{pin}    (fallback AT+PSWD={pin})")
        print("  - AT+BAUDx (depends on baud map)")


# -------------------------
# CLI parser
# -------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect and configure HC-05 / HC-06 modules via AT commands.",
    )

    # legacy/root single-setup options (still supported without subcommand)
    parser.add_argument("--port", help="Serial port (COM3, /dev/ttyUSB0, etc.)")
    parser.add_argument(
        "--module",
        default="auto",
        choices=["auto", "hc05", "hc06"],
        help="Force module type; auto tries to detect.",
    )
    parser.add_argument("--name", help="Bluetooth name to set (optional).")
    parser.add_argument(
        "--pin",
        help="4-digit PIN (optional; will try AT+PSWD / AT+PIN depending on module).",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=9600,
        help="Desired data-mode baud after setup (default: 9600).",
    )
    parser.add_argument(
        "--role",
        choices=["slave", "master"],
        default="slave",
        help="HC-05 only: role to set (slave/master).",
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Only detect module and AT profile, then exit.",
    )
    parser.add_argument(
        "--show-plan",
        action="store_true",
        help="Print checklist/plan before running (single setup only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Single setup: print plan then exit (requires --show-plan).",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available serial ports and exit.",
    )

    sub = parser.add_subparsers(dest="command")

    pair = sub.add_parser("pair", help="Auto pair one master + one slave.")
    pair.add_argument(
        "--mode",
        required=True,
        choices=["one", "two"],
        help="Pairing mode: one-port swap or two-port.",
    )
    pair.add_argument("--port", help="Serial port for mode=one (shared).")
    pair.add_argument("--master-port", help="Serial port for MASTER (mode=two).")
    pair.add_argument("--slave-port", help="Serial port for SLAVE (mode=two).")
    pair.add_argument("--name-master", help="Name to set on MASTER (optional).")
    pair.add_argument("--name-slave", help="Name to set on SLAVE (optional).")
    pair.add_argument(
        "--pin",
        default="1234",
        help="4-digit PIN to set on both modules (default: 1234).",
    )
    pair.add_argument(
        "--baud",
        type=int,
        default=9600,
        help="Desired data-mode baud after setup (default: 9600).",
    )
    pair.add_argument(
        "--list-ports",
        action="store_true",
        help="List available serial ports and exit.",
    )
    pair.add_argument(
        "--no-orig",
        action="store_true",
        default=None,
        help="Skip AT+ORGL on SLAVE (if firmware lacks it).",
    )
    pair.add_argument(
        "--no-rmaad",
        action="store_true",
        default=None,
        help="Skip AT+RMAAD on MASTER (if firmware lacks it).",
    )
    pair.add_argument(
        "--advanced",
        action="store_true",
        default=None,
        help="Advanced interactive command selection (skip steps / add extra).",
    )
    pair.add_argument(
        "--show-plan",
        action="store_true",
        default=None,
        help="Show plan (even if not advanced).",
    )
    pair.add_argument(
        "--basic",
        dest="basic",
        action="store_const",
        const=True,
        default=None,
        help="Run basic sequence (default).",
    )
    pair.add_argument(
        "--no-basic",
        dest="basic",
        action="store_const",
        const=False,
        help="Disable basic sequence (only extra/custom).",
    )
    pair.add_argument(
        "--skip-steps",
        help='Comma-separated steps to skip (e.g. "orlg,rmaad,init,reset").',
    )
    pair.add_argument(
        "--extra-master-cmd",
        action="append",
        default=[],
        help="Extra AT command for MASTER (can repeat).",
    )
    pair.add_argument(
        "--extra-slave-cmd",
        action="append",
        default=[],
        help="Extra AT command for SLAVE (can repeat).",
    )
    pair.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Print plan only; do not send commands.",
    )
    pair.add_argument(
        "--save-profile",
        help="Save resolved flags/commands to JSON file.",
    )
    pair.add_argument(
        "--load-profile",
        help="Load flags/commands from JSON file.",
    )
    return parser


# -------------------------
# Handlers
# -------------------------

def handle_setup(args: argparse.Namespace) -> int:
    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No ports detected.")
            return 0
        print("Serial ports:")
        print_port_menu(ports)
        return 0

    # If user runs without subcommand but with no args -> menu will intercept in main().
    # Here, just ensure we have port.
    if not args.port:
        port = pick_port_interactive("setup")
        if not port:
            print("Cancelled.")
            return 2
        args.port = port

    # Validate PIN if provided via CLI.
    if args.pin and (not args.pin.isdigit() or len(args.pin) != 4):
        print("PIN must be exactly 4 digits.")
        return 1
    if args.baud <= 0:
        print("Baud must be positive.")
        return 1

    if args.detect_only:
        detection = detect_module(args.port, logger=print)
        if not detection:
            print(
                "!! Could not detect module. Check wiring (RX/TX swapped?), AT mode, "
                "baud/line ending, and try --detect-only with different settings."
            )
            return 1
        detected_type = detection.module
        print(f"Detected {detected_type.upper()} using {describe_profile(detection.profile)}")
        if detection.role_response.strip():
            print(f"ROLE? response: {detection.role_response.strip()}")
        else:
            print("ROLE? response: (no data)")
        print("Detect-only mode complete.")
        return 0

    if args.show_plan:
        _print_setup_checklist(args.module, args.name, args.pin, args.baud, args.role)
        if args.dry_run:
            print("\n(DRY-RUN) Exiting without sending commands.")
            return 0
        if not prompt_yes_no("Run setup now?", default_yes=True):
            print("Cancelled.")
            return 2

    ok, _ = run_setup(
        args.port,
        args.module,
        name=args.name,
        pin=args.pin,
        baud=args.baud,
        role=args.role,
        logger=print,
    )
    return 0 if ok else 1


def _choose_addr_cli(addrs: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    if not addrs:
        return None
    print("Select SLAVE address found via INQ:")
    for idx, addr in enumerate(addrs, start=1):
        print(f"[{idx}] {addr[0]} (use: {addr[1]})")
    attempts = 0
    while attempts < 3:
        choice = input(f"Choose (1-{len(addrs)}) or Enter to cancel: ").strip()
        if choice == "":
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(addrs):
                return addrs[idx - 1]
        attempts += 1
        print("Invalid choice.")
    return None


def handle_pair(args: argparse.Namespace) -> int:
    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No ports detected.")
            return 0
        print("Serial ports:")
        print_port_menu(ports)
        return 0

    mode = args.mode.lower()

    # Resolve ports + interactive pick if missing
    if mode == "one":
        shared = args.port or args.master_port or args.slave_port
        if shared:
            master_port = slave_port = shared
        else:
            port = pick_port_interactive("pair (one-port swap)")
            if not port:
                print("Cancelled.")
                return 2
            master_port = slave_port = port
    else:
        master_port = args.master_port
        slave_port = args.slave_port
        if master_port and slave_port and master_port == slave_port:
            print("MASTER and SLAVE ports must differ in mode=two.")
            return 1
        if not master_port:
            master_port = pick_port_interactive("pair MASTER", exclude={slave_port} if slave_port else set())
            if not master_port:
                print("Cancelled.")
                return 2
        if not slave_port:
            slave_port = pick_port_interactive("pair SLAVE", exclude={master_port} if master_port else set())
            if not slave_port:
                print("Cancelled.")
                return 2
        if master_port == slave_port:
            print("MASTER and SLAVE ports must differ.")
            return 1

    # Prompt missing “user input” in CLI (the user asked: PIN/PSWD should be asked)
    if args.name_master is None:
        args.name_master = prompt_input("Name MASTER (optional)", "")
    if args.name_slave is None:
        args.name_slave = prompt_input("Name SLAVE (optional)", "")
    if args.pin is None:
        args.pin = prompt_input("PIN (4 digits)", "1234")
    if args.baud is None:
        args.baud = int(prompt_input("Data-mode baud", "9600"))

    # Validate PIN/baud
    if args.pin and (not args.pin.isdigit() or len(args.pin) != 4):
        print("PIN must be exactly 4 digits.")
        return 1
    if args.baud <= 0:
        print("Baud must be positive.")
        return 1

    flags = PairFlags()
    if args.load_profile:
        loaded = _load_profile_file(Path(args.load_profile))
        if loaded:
            flags = loaded

    if args.basic is not None:
        flags.basic = args.basic
    if args.no_orig is not None:
        flags.no_orlg = args.no_orig
    if args.no_rmaad is not None:
        flags.no_rmaad = args.no_rmaad
    if args.skip_steps:
        flags.skip_steps |= _parse_skip_steps(args.skip_steps)
    if args.extra_master_cmd:
        flags.extra_master_cmds.extend(args.extra_master_cmd)
    if args.extra_slave_cmd:
        flags.extra_slave_cmds.extend(args.extra_slave_cmd)

    # IMPORTANT: make --advanced truly interactive (so it asks skip/add extra)
    if args.advanced:
        flags.advanced = True
        flags.interactive = True

    if args.dry_run:
        flags.dry_run = True
    if args.show_plan:
        flags.show_plan = True

    def prompt_swap(msg: str, default_port: str) -> str:
        print(f"{msg}\n(Press Enter to continue)")
        input()
        return default_port

    result = run_pair(
        mode=mode,
        master_port=master_port,
        slave_port=slave_port,
        port=args.port,
        name_master=(args.name_master or None),
        name_slave=(args.name_slave or None),
        pin=args.pin,
        baud=args.baud,
        flags=flags,
        prompt_swap=prompt_swap,
        choose_addr_cb=_choose_addr_cli,
        logger=print,
        return_flags=bool(args.save_profile),
    )

    if args.save_profile:
        ok, slave_flags, master_flags = result
        _save_profile_file(Path(args.save_profile), slave_flags, master_flags)
    else:
        ok = result

    return 0 if ok else 1


# -------------------------
# Main interactive menu
# -------------------------

def interactive_menu(parser: argparse.ArgumentParser) -> int:
    print("HC-05 / HC-06 Setup Wizard (CLI)")
    print("Tip: Press Enter to choose the default.\n")
    print("[1] Pair MASTER/SLAVE (recommended)")
    print("[2] Single Setup (one device)")
    print("[3] List ports")
    print("[4] Exit")

    choice = input("Choose [1-4] (default=1): ").strip()
    if choice == "":
        choice = "1"

    if choice == "3":
        ports = list_serial_ports()
        if not ports:
            print("No ports detected.")
            return 0
        print("Serial ports:")
        print_port_menu(ports)
        return 0

    if choice == "4":
        print("Bye.")
        return 0

    if choice == "2":
        args = parser.parse_args(["--show-plan"])  # base defaults
        filled = wizard_setup_fill(args)
        if not filled:
            print("Cancelled.")
            return 2
        # show plan + ask confirm already in handler
        return handle_setup(filled)

    # default = pair
    # Build a dummy args namespace for pair wizard (avoid requiring subcommand syntax)
    args = argparse.Namespace(
        command="pair",
        mode="two",
        port=None,
        master_port=None,
        slave_port=None,
        name_master=None,
        name_slave=None,
        pin="1234",
        baud=9600,
        list_ports=False,
        no_orig=None,
        no_rmaad=None,
        advanced=None,
        show_plan=None,
        basic=None,
        skip_steps=None,
        extra_master_cmd=[],
        extra_slave_cmd=[],
        dry_run=None,
        save_profile=None,
        load_profile=None,
    )
    filled = wizard_pair_fill(args)
    if not filled:
        print("Cancelled.")
        return 2
    return handle_pair(filled)


def main() -> int:
    parser = build_parser()

    # If user ran: `python tools/hc_setup_wizard.py` with no args
    if len(sys.argv) == 1:
        return interactive_menu(parser)

    args = parser.parse_args()

    # No subcommand -> treat as single setup (legacy behavior)
    if args.command is None:
        # If user passed only flags and still wants prompts: they can just omit them or use menu.
        return handle_setup(args)

    if args.command == "pair":
        return handle_pair(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
