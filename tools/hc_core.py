#!/usr/bin/env python3
"""
Shared logic for HC-05 / HC-06 detection, setup, and (optional) pairing.

Stability fixes:
- Per-step timeout_ms / quiet_gap_ms for AT commands (PAIR can take ~20s).
- For pairing:
  - AT+PAIR is OPTIONAL by default (many firmwares return ERROR:(16)).
  - In mode ONE (one-port swap), AT+LINK is OPTIONAL (SLAVE may be unpowered during MASTER phase).
  - Tool will PASS when MASTER is configured (BIND done) even if PAIR/LINK fail in mode ONE,
    and will print clear next-steps: power both modules in DATA mode to auto-connect.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple

import serial
from serial import Serial, SerialException
from serial.tools import list_ports


Logger = Callable[[str], None]

LINE_ENDINGS = {"crlf": b"\r\n", "none": b""}


@dataclass
class SerialProfile:
    baud: int
    line_ending: str  # "crlf" or "none"


DETECTION_PROFILES = [
    SerialProfile(baud=38400, line_ending="crlf"),  # common HC-05 AT mode
    SerialProfile(baud=9600, line_ending="none"),  # common HC-06 AT mode
    SerialProfile(baud=38400, line_ending="none"),  # fallback
    SerialProfile(baud=9600, line_ending="crlf"),  # fallback
]


@dataclass
class DetectionResult:
    module: str  # "hc05" or "hc06" (best-effort)
    profile: SerialProfile
    role_response: str


def describe_profile(profile: SerialProfile) -> str:
    ending = profile.line_ending.upper()
    return f"{profile.baud} baud, line ending {ending}"


def list_serial_ports() -> List[list_ports.ListPortInfo]:
    return list(list_ports.comports())


def format_port_entry(port_info: list_ports.ListPortInfo) -> str:
    parts = [port_info.device]
    desc = port_info.description
    hwid = port_info.hwid
    if desc and desc != port_info.device:
        parts.append(f"- {desc}")
    if hwid:
        parts.append(f"({hwid})")
    return " ".join(parts)


def read_response(
    ser: Serial, *, timeout_ms: int = 2000, quiet_gap_ms: int = 200
) -> str:
    start = time.time()
    last_data = start
    chunks = bytearray()

    timeout_s = max(0.1, timeout_ms / 1000.0)
    quiet_gap_s = max(0.01, quiet_gap_ms / 1000.0)

    while time.time() - start < timeout_s:
        waiting = ser.in_waiting
        data = ser.read(waiting or 1)
        if data:
            chunks.extend(data)
            last_data = time.time()
            continue
        if chunks and (time.time() - last_data) >= quiet_gap_s:
            break

    try:
        return chunks.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def send_command(
    ser: Serial,
    command: str,
    profile: SerialProfile,
    *,
    expect_ok: bool = True,
    retries: int = 1,
    timeout_ms: int = 2000,
    quiet_gap_ms: int = 200,
    logger: Logger = print,
    stop_event=None,
) -> Tuple[bool, str]:
    response: str = ""
    line_bytes = LINE_ENDINGS[profile.line_ending]

    for attempt in range(1, retries + 1):
        if stop_event is not None and stop_event.is_set():
            logger(".. cancelled")
            return False, response

        ser.reset_input_buffer()
        time.sleep(0.05)

        payload = command.encode("ascii", errors="ignore") + line_bytes
        logger(f">> {command} ({describe_profile(profile)})")
        try:
            ser.write(payload)
            ser.flush()
        except SerialException as exc:
            logger(f"!! Write failed: {exc}")
            return False, response

        response = read_response(ser, timeout_ms=timeout_ms, quiet_gap_ms=quiet_gap_ms)
        if response.strip():
            logger(f"<< {response.strip()}")
        else:
            logger("<< (no response)")

        if expect_ok:
            if "OK" in response.upper():
                return True, response
        else:
            return True, response

        if attempt < retries:
            logger(".. retrying ..")
            time.sleep(0.25)

    return False, response


def _prefixed_logger(logger: Logger, prefix: str) -> Logger:
    return lambda msg: logger(f"[{prefix}] {msg}")


def parse_addr_response(resp: str) -> Optional[Tuple[str, str]]:
    """
    Parse address from responses like '+ADDR:1234:56:ABCDEF' or '+INQ:1234:56:ABCDEF,...'
    Returns (colon_format, comma_format)
    """
    import re

    m = re.search(r"([0-9A-F]{4}):([0-9A-F]{2}):([0-9A-F]{6})", resp, re.IGNORECASE)
    if not m:
        return None
    a, b, c = m.groups()
    colon = f"{a}:{b}:{c}"
    comma = f"{a},{b},{c}"
    return colon, comma


def detect_module(
    port: str,
    profiles: List[SerialProfile] = None,
    *,
    logger: Logger = print,
    stop_event=None,
) -> Optional[DetectionResult]:
    profiles = profiles or DETECTION_PROFILES
    for profile in profiles:
        if stop_event is not None and stop_event.is_set():
            logger(".. detect cancelled")
            return None

        logger(f"Probing {port} with {describe_profile(profile)} ...")
        try:
            with serial.Serial(
                port,
                baudrate=profile.baud,
                timeout=0.6,
                write_timeout=1,
            ) as ser:
                ok, _ = send_command(
                    ser,
                    "AT",
                    profile,
                    expect_ok=True,
                    retries=2,
                    timeout_ms=2000,
                    logger=logger,
                    stop_event=stop_event,
                )
                if not ok:
                    continue

                _, role_resp = send_command(
                    ser,
                    "AT+ROLE?",
                    profile,
                    expect_ok=False,
                    timeout_ms=2000,
                    logger=logger,
                    stop_event=stop_event,
                )
                if "ROLE" in role_resp.upper():
                    return DetectionResult(module="hc05", profile=profile, role_response=role_resp)
                return DetectionResult(module="hc06", profile=profile, role_response=role_resp)

        except SerialException as exc:
            logger(f"!! Could not open {port}: {exc}")
            break
    return None


HC06_BAUD_MAP = {
    1200: "1",
    2400: "2",
    4800: "3",
    9600: "4",
    19200: "5",
    38400: "6",
    57600: "7",
    115200: "8",
    230400: "9",
    460800: "A",
    921600: "B",
    1382400: "C",
}


def configure_hc05(
    port: str,
    profile: SerialProfile,
    *,
    name: Optional[str],
    pin: Optional[str],
    baud: int,
    role: str,
    logger: Logger = print,
    stop_event=None,
) -> bool:
    try:
        with serial.Serial(port, baudrate=profile.baud, timeout=0.6, write_timeout=1) as ser:
            ok, _ = send_command(ser, "AT", profile, retries=2, logger=logger, stop_event=stop_event)
            if not ok:
                logger("!! HC-05 did not confirm AT. Check AT mode wiring and baud/ending.")
                return False

            if stop_event is not None and stop_event.is_set():
                logger(".. cancelled")
                return False

            if name:
                ok, _ = send_command(ser, f"AT+NAME={name}", profile, logger=logger, stop_event=stop_event)
                if not ok:
                    return False

            if stop_event is not None and stop_event.is_set():
                logger(".. cancelled")
                return False

            if pin:
                ok, _ = send_command(ser, f"AT+PSWD={pin}", profile, logger=logger, stop_event=stop_event)
                if not ok:
                    logger(".. AT+PSWD failed; trying AT+PIN=<pin>")
                    ok, _ = send_command(ser, f"AT+PIN={pin}", profile, logger=logger, stop_event=stop_event)
                if not ok:
                    logger("!! PIN set failed.")
                    return False

            if stop_event is not None and stop_event.is_set():
                logger(".. cancelled")
                return False

            ok, _ = send_command(ser, f"AT+UART={baud},0,0", profile, logger=logger, stop_event=stop_event)
            if not ok:
                return False

            role_val = 0 if role == "slave" else 1
            ok, _ = send_command(ser, f"AT+ROLE={role_val}", profile, logger=logger, stop_event=stop_event)
            if not ok:
                return False

            # Some firmwares are silent on RESET.
            send_command(ser, "AT+RESET", profile, expect_ok=False, logger=logger, stop_event=stop_event)
            logger("HC-05 setup done. If the module does not restart, disconnect/reconnect power.")
            return True

    except SerialException as exc:
        logger(f"!! Serial error during HC-05 setup: {exc}")
        return False


def configure_hc06(
    port: str,
    profile: SerialProfile,
    *,
    name: Optional[str],
    pin: Optional[str],
    baud: int,
    logger: Logger = print,
    stop_event=None,
) -> bool:
    try:
        with serial.Serial(port, baudrate=profile.baud, timeout=0.6, write_timeout=1) as ser:
            ok, _ = send_command(ser, "AT", profile, retries=2, logger=logger, stop_event=stop_event)
            if not ok:
                logger("!! HC-06 did not confirm AT. Check wiring and baud/ending.")
                return False

            if stop_event is not None and stop_event.is_set():
                logger(".. cancelled")
                return False

            if name:
                ok, _ = send_command(ser, f"AT+NAME{name}", profile, logger=logger, stop_event=stop_event)
                if not ok:
                    logger(".. NAME without '=' failed; trying AT+NAME=<name>")
                    ok, _ = send_command(ser, f"AT+NAME={name}", profile, logger=logger, stop_event=stop_event)
                if not ok:
                    return False

            if stop_event is not None and stop_event.is_set():
                logger(".. cancelled")
                return False

            if pin:
                ok, _ = send_command(ser, f"AT+PIN{pin}", profile, logger=logger, stop_event=stop_event)
                if not ok:
                    logger(".. PINxxxx failed; trying AT+PSWD=xxxx")
                    ok, _ = send_command(ser, f"AT+PSWD={pin}", profile, logger=logger, stop_event=stop_event)
                if not ok:
                    return False

            if stop_event is not None and stop_event.is_set():
                logger(".. cancelled")
                return False

            if baud not in HC06_BAUD_MAP:
                logger(
                    f"!! Baud {baud} not in common HC-06 BAUD table. "
                    "Firmware mappings differ; try a supported value or set manually."
                )
                return False

            code = HC06_BAUD_MAP[baud]
            ok, _ = send_command(ser, f"AT+BAUD{code}", profile, logger=logger, stop_event=stop_event)
            if not ok:
                logger("!! Baud change command did not return OK.")
                return False

            logger("HC-06 setup done. If baud changed, reconnect at the new data-mode speed.")
            return True

    except SerialException as exc:
        logger(f"!! Serial error during HC-06 setup: {exc}")
        return False


def run_setup(
    port: str,
    module: str,
    *,
    name: Optional[str],
    pin: Optional[str],
    baud: int,
    role: str,
    logger: Logger = print,
    stop_event=None,
) -> Tuple[bool, Optional[DetectionResult]]:
    detection = detect_module(port, logger=logger, stop_event=stop_event)
    if not detection:
        logger(
            "!! Could not detect module. Check wiring (RX/TX swapped?), AT mode, "
            "baud/line ending, and try --detect-only."
        )
        return False, None

    detected_type = detection.module
    logger(f"Detected {detected_type.upper()} using {describe_profile(detection.profile)}")
    if detection.role_response.strip():
        logger(f"ROLE? response: {detection.role_response.strip()}")
    else:
        logger("ROLE? response: (no data)")

    module_to_use = module if module != "auto" else detected_type
    if module_to_use != detected_type:
        logger(f"Warning: user forced module={module_to_use}, but detection suggested {detected_type}.")

    if module_to_use == "hc05":
        ok = configure_hc05(
            port,
            detection.profile,
            name=name,
            pin=pin,
            baud=baud,
            role=role,
            logger=logger,
            stop_event=stop_event,
        )
    else:
        ok = configure_hc06(
            port,
            detection.profile,
            name=name,
            pin=pin,
            baud=baud,
            logger=logger,
            stop_event=stop_event,
        )

    return ok, detection


# ---------- Pairing helpers ----------

PAIR_CACHE_FILE = Path(__file__).resolve().parent / ".pair_cache.json"


@dataclass
class Step:
    id: str
    label: str
    command: str
    value: Optional[str] = None
    critical: bool = False
    optional: bool = False
    expect_ok: bool = True
    retries: int = 1
    capture_response: bool = False
    kind: str = "command"  # command | inq
    category: str = "basic"  # basic | extra
    timeout_ms: int = 2000
    quiet_gap_ms: int = 200


@dataclass
class PairFlags:
    basic: bool = True
    skip_steps: Set[str] = field(default_factory=set)
    extra_master_cmds: List[str] = field(default_factory=list)
    extra_slave_cmds: List[str] = field(default_factory=list)
    advanced: bool = False
    interactive: bool = False
    dry_run: bool = False
    show_plan: bool = False
    no_orlg: bool = False
    no_rmaad: bool = False
    no_pair: bool = False  # NEW: allow disabling AT+PAIR entirely
    no_link: bool = False  # NEW: allow disabling AT+LINK entirely


@dataclass
class PairContext:
    slave_addr: Optional[Tuple[str, str]] = None
    master_bind_ok: bool = False
    master_link_ok: bool = False


class PairPlanError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_pair_cache(addr_colon: str, addr_comma: str, meta: dict) -> None:
    payload = {
        "slave_addr_colon": addr_colon,
        "slave_addr_comma": addr_comma,
        "meta": meta,
        "timestamp": _now_iso(),
    }
    try:
        PAIR_CACHE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_last_slave() -> Optional[Tuple[str, str]]:
    if not PAIR_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(PAIR_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get("slave_addr_colon"), data.get("slave_addr_comma")
    except Exception:
        return None


def _should_include_step(step: Step, flags: PairFlags) -> bool:
    if step.category == "basic" and not flags.basic:
        return False
    if step.id in flags.skip_steps:
        if step.critical and flags.basic:
            raise PairPlanError(f"Cannot skip critical step: {step.label}")
        return False
    return True


def _log_plan(prefix: str, steps: List[Step], logger: Logger) -> None:
    basic_steps = [s for s in steps if s.category == "basic"]
    extra_steps = [s for s in steps if s.category != "basic"]
    logger(f"[{prefix}] BASIC STEPS:")
    for idx, step in enumerate(basic_steps, start=1):
        opt = " (optional)" if step.optional else ""
        logger(f"  {idx}) {step.label}{opt}")
    if extra_steps:
        logger(f"[{prefix}] EXTRA STEPS:")
        for step in extra_steps:
            logger(f"  - {step.label}")


def build_slave_plan(
    detection: DetectionResult,
    *,
    name: Optional[str],
    pin: Optional[str],
    baud: int,
    flags: PairFlags,
    require_addr: bool,
) -> List[Step]:
    steps: List[Step] = []
    module = detection.module

    if flags.basic:
        steps.append(Step("at", "AT", "AT", critical=True, retries=2, timeout_ms=2000))
        if module == "hc05" and not flags.no_orlg:
            steps.append(Step("orlg", "AT+ORGL", "AT+ORGL", optional=True, timeout_ms=3000))
        if module == "hc05":
            steps.append(Step("role0", "AT+ROLE=0", "AT+ROLE=0", critical=True, timeout_ms=2000))
        if name:
            steps.append(Step("name", f"AT+NAME={name}", f"AT+NAME={name}", value=name, timeout_ms=2000))
        if pin:
            steps.append(Step("pin", f"AT+PSWD={pin}", f"AT+PSWD={pin}", value=pin, timeout_ms=2000))

        if module == "hc05":
            steps.append(Step("uart", f"AT+UART={baud},0,0", f"AT+UART={baud},0,0", critical=True, timeout_ms=2000))
            steps.append(
                Step(
                    "addr",
                    "AT+ADDR?",
                    "AT+ADDR?",
                    critical=require_addr,
                    optional=not require_addr,
                    expect_ok=False,
                    capture_response=True,
                    timeout_ms=2000,
                )
            )
        else:
            if baud not in HC06_BAUD_MAP:
                raise PairPlanError(
                    f"Baud {baud} not supported by HC-06 BAUD map (choose one of: {', '.join(map(str, HC06_BAUD_MAP.keys()))})."
                )
            steps.append(
                Step("uart", f"AT+BAUD{HC06_BAUD_MAP[baud]}", f"AT+BAUD{HC06_BAUD_MAP[baud]}", critical=True, timeout_ms=2000)
            )
            steps.append(
                Step(
                    "addr",
                    "AT+ADDR?",
                    "AT+ADDR?",
                    critical=False,
                    optional=True,
                    expect_ok=False,
                    capture_response=True,
                    timeout_ms=2000,
                )
            )

    filtered: List[Step] = []
    for step in steps:
        if _should_include_step(step, flags):
            filtered.append(step)

    for idx, cmd in enumerate(flags.extra_slave_cmds, start=1):
        filtered.append(
            Step(
                id=f"extra-slave-{idx}",
                label=f"Extra (slave) {cmd}",
                command=cmd,
                expect_ok=False,
                optional=True,
                category="extra",
                timeout_ms=2500,
            )
        )

    return filtered


def build_master_plan(
    detection: DetectionResult,
    *,
    name: Optional[str],
    pin: Optional[str],
    baud: int,
    flags: PairFlags,
    slave_addr: Optional[Tuple[str, str]],
    want_scan: bool,
    require_link: bool,
) -> List[Step]:
    if detection.module != "hc05":
        raise PairPlanError("Master must be HC-05 (needs ROLE/PAIR/BIND/LINK).")

    addr_text = slave_addr[1] if slave_addr else "{addr}"
    steps: List[Step] = []

    if flags.basic:
        steps.append(Step("at", "AT", "AT", critical=True, retries=2, timeout_ms=2000))
        steps.append(Step("role1", "AT+ROLE=1", "AT+ROLE=1", critical=True, timeout_ms=2000))
        steps.append(Step("cmode", "AT+CMODE=0", "AT+CMODE=0", critical=True, timeout_ms=2000))

        if name:
            steps.append(Step("name", f"AT+NAME={name}", f"AT+NAME={name}", value=name, timeout_ms=2000))
        if pin:
            steps.append(Step("pin", f"AT+PSWD={pin}", f"AT+PSWD={pin}", value=pin, timeout_ms=2000))

        steps.append(Step("uart", f"AT+UART={baud},0,0", f"AT+UART={baud},0,0", critical=True, timeout_ms=2000))

        if not flags.no_rmaad:
            steps.append(Step("rmaad", "AT+RMAAD", "AT+RMAAD", optional=True, timeout_ms=5000))
        steps.append(Step("init", "AT+INIT", "AT+INIT", optional=True, timeout_ms=8000))

        if want_scan and not slave_addr:
            steps.append(
                Step(
                    "inq",
                    "AT+INQ (scan + pick slave)",
                    "AT+INQ",
                    critical=True,
                    expect_ok=False,
                    kind="inq",
                    timeout_ms=9000,
                )
            )

        # IMPORTANT: AT+PAIR is OPTIONAL (firmware dependent)
        if not flags.no_pair:
            steps.append(
                Step(
                    "pair",
                    f"AT+PAIR={addr_text},20",
                    f"AT+PAIR={addr_text},20",
                    critical=False,
                    optional=True,
                    timeout_ms=25000,  # must be >= 20s
                    quiet_gap_ms=300,
                )
            )

        # BIND is the key that makes data-mode auto connect reliable
        steps.append(
            Step(
                "bind",
                f"AT+BIND={addr_text}",
                f"AT+BIND={addr_text}",
                critical=True,
                timeout_ms=4000,
            )
        )

        # LINK can fail in mode ONE if SLAVE is unpowered after swap
        if not flags.no_link:
            steps.append(
                Step(
                    "link",
                    f"AT+LINK={addr_text}",
                    f"AT+LINK={addr_text}",
                    critical=require_link,
                    optional=not require_link,
                    timeout_ms=15000,
                    quiet_gap_ms=300,
                )
            )

        steps.append(Step("reset", "AT+RESET", "AT+RESET", optional=True, expect_ok=False, timeout_ms=3000))

    filtered: List[Step] = []
    for step in steps:
        if _should_include_step(step, flags):
            filtered.append(step)

    for idx, cmd in enumerate(flags.extra_master_cmds, start=1):
        filtered.append(
            Step(
                id=f"extra-master-{idx}",
                label=f"Extra (master) {cmd}",
                command=cmd,
                expect_ok=False,
                optional=True,
                category="extra",
                timeout_ms=3000,
            )
        )

    return filtered


def _inquire_addresses(
    ser: Serial,
    profile: SerialProfile,
    logger: Logger,
    stop_event=None,
    scan_seconds: float = 8.0,
) -> List[Tuple[str, str]]:
    ser.reset_input_buffer()
    payload = b"AT+INQ" + LINE_ENDINGS[profile.line_ending]
    logger(">> AT+INQ")
    try:
        ser.write(payload)
        ser.flush()
    except SerialException as exc:
        logger(f"!! Write failed: {exc}")
        return []

    buf = ""
    start = time.time()
    while time.time() - start < scan_seconds:
        if stop_event is not None and stop_event.is_set():
            logger(".. cancelled")
            break
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode("utf-8", errors="ignore")
            if chunk:
                logger(f"<< {chunk.strip()}")
                buf += chunk
        time.sleep(0.2)

    addrs: List[Tuple[str, str]] = []
    for line in buf.splitlines():
        parsed = parse_addr_response(line)
        if parsed:
            addrs.append(parsed)
    return addrs


def _execute_step(
    ser: Serial,
    profile: SerialProfile,
    step: Step,
    *,
    module: str,
    context: PairContext,
    choose_addr_cb: Optional[Callable[[List[Tuple[str, str]]], Optional[Tuple[str, str]]]],
    logger: Logger,
    stop_event,
    name_value: Optional[str],
    pin_value: Optional[str],
    baud_value: int,
) -> bool:
    if stop_event is not None and stop_event.is_set():
        logger(".. cancelled")
        return False

    if step.kind == "inq":
        if context.slave_addr:
            logger(".. address already known; skipping INQ.")
            return True
        addrs = _inquire_addresses(ser, profile, logger=logger, stop_event=stop_event)
        if not addrs:
            logger("!! No devices found via INQ.")
            return False
        selected = choose_addr_cb(addrs) if choose_addr_cb else addrs[0]
        if not selected:
            logger("!! No address selected.")
            return False
        context.slave_addr = selected
        logger(f".. Selected {selected[0]} (use: {selected[1]})")
        return True

    cmd = step.command
    if "{addr}" in cmd:
        if not context.slave_addr:
            logger("!! Missing slave address; cannot continue.")
            return False
        cmd = cmd.replace("{addr}", context.slave_addr[1])

    # Special handling: pin/name/uart/addr for compatibility
    if step.id == "pin":
        if pin_value is None:
            return True
        if module == "hc05":
            ok, _ = send_command(
                ser, f"AT+PSWD={pin_value}", profile, logger=logger, stop_event=stop_event,
                timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
            )
            if not ok:
                logger(".. AT+PSWD failed; trying AT+PIN=<pin>")
                ok, _ = send_command(
                    ser, f"AT+PIN={pin_value}", profile, logger=logger, stop_event=stop_event,
                    timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
                )
        else:
            ok, _ = send_command(
                ser, f"AT+PIN{pin_value}", profile, logger=logger, stop_event=stop_event,
                timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
            )
            if not ok:
                logger(".. AT+PINxxxx failed; trying AT+PSWD=xxxx")
                ok, _ = send_command(
                    ser, f"AT+PSWD={pin_value}", profile, logger=logger, stop_event=stop_event,
                    timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
                )
        if not ok and step.optional:
            logger(".. PIN step skipped (optional).")
            return True
        return ok

    if step.id == "name":
        if name_value is None:
            return True
        if module == "hc06":
            ok, _ = send_command(
                ser, f"AT+NAME{name_value}", profile, logger=logger, stop_event=stop_event,
                timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
            )
            if not ok:
                logger(".. NAME without '=' failed; trying AT+NAME=<name>")
                ok, _ = send_command(
                    ser, f"AT+NAME={name_value}", profile, logger=logger, stop_event=stop_event,
                    timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
                )
        else:
            ok, _ = send_command(
                ser, cmd, profile, logger=logger, stop_event=stop_event,
                timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
            )
        if not ok and step.optional:
            logger(".. NAME step skipped (optional).")
            return True
        return ok

    if step.id == "uart":
        if module == "hc06":
            if baud_value not in HC06_BAUD_MAP:
                logger(f"!! Baud {baud_value} not supported by HC-06 auto map.")
                return False
            cmd = f"AT+BAUD{HC06_BAUD_MAP[baud_value]}"
        else:
            cmd = f"AT+UART={baud_value},0,0"
        ok, _ = send_command(
            ser, cmd, profile, expect_ok=step.expect_ok, logger=logger, stop_event=stop_event,
            retries=step.retries, timeout_ms=step.timeout_ms, quiet_gap_ms=step.quiet_gap_ms
        )
        if not ok and step.optional:
            logger(".. UART step skipped (optional).")
            return True
        return ok

    if step.id == "addr":
        ok, resp = send_command(
            ser,
            cmd,
            profile,
            expect_ok=False,
            retries=step.retries,
            logger=logger,
            stop_event=stop_event,
            timeout_ms=step.timeout_ms,
            quiet_gap_ms=step.quiet_gap_ms,
        )
        parsed = parse_addr_response(resp)
        if parsed:
            context.slave_addr = parsed
            logger(f"SLAVE ADDRESS: {parsed[0]}  (use: {parsed[1]})")
            return True
        if step.critical:
            logger("!! Mode one needs the SLAVE address (recommend SLAVE as HC-05).")
            return False
        logger(".. Could not read slave address; master will rely on INQ.")
        return True

    ok, resp = send_command(
        ser,
        cmd,
        profile,
        expect_ok=step.expect_ok,
        retries=step.retries,
        logger=logger,
        stop_event=stop_event,
        timeout_ms=step.timeout_ms,
        quiet_gap_ms=step.quiet_gap_ms,
    )

    # Track BIND/LINK outcome for final message
    if step.id == "bind" and ok:
        context.master_bind_ok = True
    if step.id == "link" and ok:
        context.master_link_ok = True

    if not ok and step.optional:
        # Show firmware error but continue
        if resp.strip():
            logger(f".. Optional step '{step.label}' failed; continuing.")
        else:
            logger(f".. Optional step '{step.label}' no response; continuing.")
        return True

    return ok


def _run_plan_on_port(
    port: str,
    detection: DetectionResult,
    steps: List[Step],
    *,
    flags: PairFlags,
    context: PairContext,
    logger: Logger,
    choose_addr_cb: Optional[Callable[[List[Tuple[str, str]]], Optional[Tuple[str, str]]]],
    stop_event=None,
    name_value: Optional[str],
    pin_value: Optional[str],
    baud_value: int,
) -> bool:
    profile = detection.profile
    logger(f"Using profile {describe_profile(profile)} on {port}")

    if flags.dry_run:
        logger(".. DRY-RUN: skipping serial writes.")
        return True

    try:
        with serial.Serial(port, baudrate=profile.baud, timeout=0.8, write_timeout=1) as ser:
            for step in steps:
                ok = _execute_step(
                    ser,
                    profile,
                    step,
                    module=detection.module,
                    context=context,
                    choose_addr_cb=choose_addr_cb,
                    logger=logger,
                    stop_event=stop_event,
                    name_value=name_value,
                    pin_value=pin_value,
                    baud_value=baud_value,
                )
                if not ok:
                    return False
        return True
    except SerialException as exc:
        logger(f"!! Serial error on port {port}: {exc}")
        return False


def _interactive_tune(phase: str, steps: List[Step], flags: PairFlags, *, logger: Logger) -> PairFlags:
    basic_steps = [s for s in steps if s.category == "basic"]
    if not basic_steps:
        return flags

    while True:
        logger(f"[{phase}] Choose plan (a=all, b=skip steps, c=no-basic, d=add extra).")
        choice = input(f"{phase} choice [a/b/c/d]: ").strip().lower()
        if choice in ("", "a"):
            break
        if choice == "c":
            flags.basic = False
            break
        if choice == "d":
            logger(f"[{phase}] Enter extra commands (one per line, blank to finish):")
            extras: List[str] = []
            while True:
                line = input().strip()
                if not line:
                    break
                extras.append(line)
            if phase.upper() == "SLAVE":
                flags.extra_slave_cmds.extend(extras)
            else:
                flags.extra_master_cmds.extend(extras)
        if choice == "b":
            nums = input(f"{phase} skip steps (e.g. 7,8,12): ").strip()
            if nums:
                for token in nums.split(","):
                    token = token.strip()
                    if not token.isdigit():
                        continue
                    idx = int(token)
                    if 1 <= idx <= len(basic_steps):
                        step = basic_steps[idx - 1]
                        if step.critical:
                            logger(f"[{phase}] !! Cannot skip critical step: {step.label}")
                            continue
                        flags.skip_steps.add(step.id)
        again = input(f"{phase} adjust more? (Y/n): ").strip().lower()
        if again not in ("y", "yes"):
            break
    return flags


def run_pair(
    *,
    mode: str,
    master_port: Optional[str],
    slave_port: Optional[str],
    port: Optional[str],
    name_master: Optional[str],
    name_slave: Optional[str],
    pin: Optional[str],
    baud: int,
    flags: Optional[PairFlags] = None,
    prompt_swap: Optional[Callable[[str, str], str]] = None,
    choose_addr_cb: Optional[Callable[[List[Tuple[str, str]]], Optional[Tuple[str, str]]]] = None,
    logger: Logger = print,
    stop_event=None,
    return_flags: bool = False,
):
    flags = flags or PairFlags()

    # Advanced implies interactive
    if flags.advanced:
        flags.interactive = True

    flags.show_plan = flags.show_plan or flags.advanced or flags.dry_run or flags.interactive

    slave_flags: PairFlags = flags
    master_flags: PairFlags = flags

    def _ret(ok: bool):
        if return_flags:
            return ok, slave_flags, master_flags
        return ok

    if pin and (not pin.isdigit() or len(pin) != 4):
        logger("PIN must be 4 digits.")
        return _ret(False)
    if baud <= 0:
        logger("Baud must be positive.")
        return _ret(False)

    actual_mode = mode.lower()
    if actual_mode not in ("one", "two"):
        logger("Mode must be 'one' or 'two'.")
        return _ret(False)

    if actual_mode == "one":
        chosen_port = port or slave_port or master_port
        if not chosen_port:
            logger("Mode one requires --port (shared) or at least one port value.")
            return _ret(False)
        slave_port = chosen_port
        master_port = chosen_port
    else:
        if not master_port or not slave_port:
            logger("Mode two requires both --master-port and --slave-port.")
            return _ret(False)
        if master_port == slave_port:
            logger("Master and slave ports must differ in mode two.")
            return _ret(False)

    context = PairContext()
    slave_flags = copy.deepcopy(flags)
    master_flags = copy.deepcopy(flags)

    # ---- SLAVE phase ----
    slave_logger = _prefixed_logger(logger, "SLAVE")
    slave_detect = detect_module(slave_port, logger=slave_logger, stop_event=stop_event)
    if not slave_detect:
        slave_logger("!! Detect failed on SLAVE.")
        return _ret(False)

    if actual_mode == "one" and slave_detect.module != "hc05":
        slave_logger("!! Mode one needs the SLAVE address (recommend SLAVE as HC-05).")
        return _ret(False)

    try:
        slave_plan = build_slave_plan(
            slave_detect,
            name=name_slave,
            pin=pin,
            baud=baud,
            flags=slave_flags,
            require_addr=(actual_mode == "one"),
        )
    except PairPlanError as exc:
        slave_logger(f"!! {exc}")
        return _ret(False)

    if slave_flags.show_plan:
        _log_plan("SLAVE", slave_plan, slave_logger)

    if slave_flags.interactive:
        slave_flags = _interactive_tune("SLAVE", slave_plan, slave_flags, logger=logger)
        try:
            slave_plan = build_slave_plan(
                slave_detect,
                name=name_slave,
                pin=pin,
                baud=baud,
                flags=slave_flags,
                require_addr=(actual_mode == "one"),
            )
        except PairPlanError as exc:
            slave_logger(f"!! {exc}")
            return _ret(False)
        if slave_flags.show_plan:
            _log_plan("SLAVE", slave_plan, slave_logger)

    slave_ok = _run_plan_on_port(
        slave_port,
        slave_detect,
        slave_plan,
        flags=slave_flags,
        context=context,
        logger=slave_logger,
        choose_addr_cb=choose_addr_cb,
        stop_event=stop_event,
        name_value=name_slave,
        pin_value=pin,
        baud_value=baud,
    )
    if not slave_ok:
        logger("[FAIL] Could not configure SLAVE.")
        return _ret(False)

    if context.slave_addr:
        _write_pair_cache(
            context.slave_addr[0],
            context.slave_addr[1],
            {"port": slave_port, "pin": pin, "baud": baud, "name_slave": name_slave, "mode": actual_mode},
        )

    if actual_mode == "one" and not context.slave_addr and not slave_flags.dry_run:
        slave_logger("!! Mode one needs the SLAVE address (recommend SLAVE as HC-05).")
        return _ret(False)

    # ---- Swap prompt (mode ONE) ----
    if actual_mode == "one" and not flags.dry_run:
        logger(
            "Unplug SLAVE from USB-UART.\n"
            "IMPORTANT: To complete LINK immediately, SLAVE should remain POWERED in DATA mode (KEY/EN LOW).\n"
            "If SLAVE is not powered, MASTER will still be configured (BIND) and will auto-connect later when both are powered."
        )
        if prompt_swap:
            master_port = prompt_swap(
                "Swap to MASTER (HC-05). Put MASTER in AT mode (KEY/EN high when powering).",
                master_port,
            ) or master_port
        else:
            input("Plug MASTER (HC-05) in AT mode, then press Enter to continue... ")

    # ---- MASTER phase ----
    master_logger = _prefixed_logger(logger, "MASTER")
    master_detect = detect_module(master_port, logger=master_logger, stop_event=stop_event)
    if not master_detect:
        master_logger("!! Detect failed on MASTER.")
        return _ret(False)
    if master_detect.module != "hc05":
        master_logger("!! MASTER must be HC-05 (ROLE/PAIR/BIND/LINK).")
        return _ret(False)

    # Mode TWO: we want LINK (device should be present, on another port)
    # Mode ONE: do NOT require LINK (SLAVE might be unpowered after swap)
    require_link = (actual_mode == "two") and (not master_flags.no_link)

    try:
        master_plan = build_master_plan(
            master_detect,
            name=name_master,
            pin=pin,
            baud=baud,
            flags=master_flags,
            slave_addr=context.slave_addr,
            want_scan=(context.slave_addr is None),
            require_link=require_link,
        )
    except PairPlanError as exc:
        master_logger(f"!! {exc}")
        return _ret(False)

    if master_flags.show_plan:
        _log_plan("MASTER", master_plan, master_logger)

    if master_flags.interactive:
        master_flags = _interactive_tune("MASTER", master_plan, master_flags, logger=logger)
        try:
            master_plan = build_master_plan(
                master_detect,
                name=name_master,
                pin=pin,
                baud=baud,
                flags=master_flags,
                slave_addr=context.slave_addr,
                want_scan=(context.slave_addr is None),
                require_link=require_link,
            )
        except PairPlanError as exc:
            master_logger(f"!! {exc}")
            return _ret(False)
        if master_flags.show_plan:
            _log_plan("MASTER", master_plan, master_logger)

    master_ok = _run_plan_on_port(
        master_port,
        master_detect,
        master_plan,
        flags=master_flags,
        context=context,
        logger=master_logger,
        choose_addr_cb=choose_addr_cb,
        stop_event=stop_event,
        name_value=name_master,
        pin_value=pin,
        baud_value=baud,
    )
    if not master_ok:
        logger("[FAIL] MASTER phase failed.")
        return _ret(False)

    # Final outcome messaging:
    if require_link:
        if context.master_link_ok:
            logger("[PASS] MASTER/SLAVE paired (LINK OK).")
            return _ret(True)
        logger("[FAIL] LINK required but did not succeed.")
        return _ret(False)

    # Mode ONE: treat as PASS if MASTER config succeeded (BIND critical)
    if context.master_link_ok:
        logger("[PASS] MASTER/SLAVE paired (LINK OK).")
    else:
        logger(
            "[PASS] MASTER configured (BIND set). LINK may fail in one-port swap if SLAVE is unpowered.\n"
            "NEXT: Power both modules in DATA mode (KEY/EN LOW). MASTER should auto-connect to the bound SLAVE."
        )
    return _ret(True)
