#!/usr/bin/env python3
"""
service.py -- manage the bot as a systemd *user* service.

Why a service instead of `nohup ... > log 2>&1`:
  * auto-restarts on crash (Restart=always),
  * survives logout/suspend (with lingering enabled),
  * logs go to the journal, so `journalctl --user -u saltybot -f` replaces tee,
  * start/stop/status without hunting for PIDs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from config import ROOT, Settings

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args])


def available() -> bool:
    """Is a usable systemd user session available?"""
    if not shutil.which("systemctl"):
        return False
    r = _run(["systemctl", "--user", "is-system-running"])
    return r.returncode in (0, 1)  # 'running' or 'degraded' are both usable


def unit_path(name: str) -> Path:
    return UNIT_DIR / f"{name}.service"


def build_unit(settings: Settings) -> str:
    python = sys.executable or "python3"
    log = settings.resolved_log_path()
    return f"""[Unit]
Description=SaltyBet trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={python} {ROOT / 'src' / 'bot.py'} --trade --config {ROOT / 'config' / 'settings.json'}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec={settings.restart_sec}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def install(settings: Settings) -> tuple[bool, str]:
    if not available():
        return False, "systemd user session not available"
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    p = unit_path(settings.service_name)
    p.write_text(build_unit(settings))
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", settings.service_name])
    return True, f"wrote {p} and enabled {settings.service_name}"


def uninstall(name: str) -> tuple[bool, str]:
    if not available():
        return False, "systemd user session not available"
    systemctl("stop", name)
    systemctl("disable", name)
    p = unit_path(name)
    if p.exists():
        p.unlink()
    _run(["systemctl", "--user", "daemon-reload"])
    return True, f"removed {name} service"


def start(name: str) -> tuple[bool, str]:
    r = systemctl("start", name)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, f"started {name}"


def stop(name: str) -> tuple[bool, str]:
    r = systemctl("stop", name)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, f"stopped {name}"


def restart(name: str) -> tuple[bool, str]:
    r = systemctl("restart", name)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, f"restarted {name}"


def status(name: str) -> tuple[bool, str]:
    r = _run(["systemctl", "--user", "status", name, "--no-pager", "--lines=15"])
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def is_active(name: str) -> bool:
    r = systemctl("is-active", name)
    return r.stdout.strip() == "active"


def logs(name: str, lines: int = 50, follow: bool = False) -> None:
    cmd = ["journalctl", "--user", "-u", name, "--no-pager", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd)


def linger_enabled() -> bool:
    r = _run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"])
    return "Linger=yes" in r.stdout


def enable_linger_hint() -> str:
    return ("Run 'sudo loginctl enable-linger $USER' so the service keeps running "
            "after you log out (and survives reboot).")
