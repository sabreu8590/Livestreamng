"""Read and control the livestream systemd units from the dashboard.

This is deliberately narrow. The web process can only touch units whose names
match a configured pattern, and only with start, stop and restart. It cannot
run arbitrary systemctl, because a password-protected web page is not a
good place to hand out a root shell.
"""

import re
import shutil
import subprocess
import time

import db

DEFAULT_PATTERN = "stream@*.service"
SAFE_UNIT = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
ACTIONS = {"start", "stop", "restart"}


def _systemctl(*args, timeout=15):
    if not shutil.which("systemctl"):
        raise RuntimeError("systemctl is not available on this machine")
    proc = subprocess.run(["systemctl", *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout)
    return proc


def pattern():
    return db.get_setting("stream_pattern", DEFAULT_PATTERN)


def set_pattern(value):
    db.set_setting("stream_pattern", str(value or DEFAULT_PATTERN))


def _allowed(unit):
    """A unit is controllable only if it is one the pattern already lists."""
    if not SAFE_UNIT.match(unit or ""):
        return False
    return unit in {s["unit"] for s in list_units()}


def list_units():
    """Every unit matching the configured pattern, with state and uptime."""
    try:
        proc = _systemctl("list-units", "--type=service", "--all",
                          "--no-legend", "--no-pager", "--plain", pattern())
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(str(exc))
    if proc.returncode != 0 and not proc.stdout.strip():
        # Distinguish "systemd is unreachable" from "nothing matched", because
        # silently reporting zero streams when the bus is down sends you
        # hunting through your unit names for a problem that is not there.
        err = (proc.stderr or "").strip()
        if err:
            raise RuntimeError(err.splitlines()[0][:300])
        return []

    units = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].endswith(".service"):
            continue
        units.append(parts[0])

    out = []
    for unit in units:
        out.append(describe(unit))
    return out


PROPS = ["ActiveState", "SubState", "UnitFileState", "ExecMainStartTimestamp",
         "ExecMainPID", "NRestarts", "Description", "ExecStart"]


def describe(unit):
    info = {"unit": unit}
    try:
        proc = _systemctl("show", unit, "--no-pager",
                          "--property=" + ",".join(PROPS))
        raw = {}
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            raw[key.strip()] = value.strip()
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
        return info

    info["state"] = raw.get("ActiveState") or "unknown"
    info["sub"] = raw.get("SubState") or ""
    info["enabled"] = raw.get("UnitFileState") or ""
    info["restarts"] = int(raw.get("NRestarts") or 0)
    info["description"] = raw.get("Description") or ""
    info["uptime"] = _uptime(raw.get("ExecMainStartTimestamp"))
    info["source"] = _source_file(raw.get("ExecStart") or "")
    info["channel"] = unit.split("@", 1)[1].rsplit(".", 1)[0] if "@" in unit else unit
    return info


def _uptime(stamp):
    if not stamp or stamp in ("n/a", "0"):
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            started = time.mktime(time.strptime(stamp, fmt))
            return max(0, time.time() - started)
        except (ValueError, OverflowError):
            continue
    return None


VIDEO_IN_CMD = re.compile(r"[^\s\"']+\.(?:mp4|mkv|mov|flv|ts|m3u8|txt)")


def _source_file(exec_start):
    """Best guess at the file a looping ffmpeg unit is playing."""
    hits = VIDEO_IN_CMD.findall(exec_start or "")
    return hits[0] if hits else None


def control(unit, action):
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")
    if not _allowed(unit):
        raise PermissionError(
            f"{unit} is not one of the units this dashboard manages "
            f"(pattern: {pattern()})")
    proc = _systemctl(action, unit, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:400]
                           or f"systemctl {action} failed")
    return describe(unit)


def journal(unit, lines=40):
    if not _allowed(unit):
        raise PermissionError(f"{unit} is not managed here")
    if not shutil.which("journalctl"):
        return "journalctl is not available"
    proc = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(max(1, min(lines, 200))),
         "--no-pager", "--output=short-iso"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    return (proc.stdout or proc.stderr or "").strip()
