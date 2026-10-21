#!/usr/bin/env python3
"""
config.py -- persistent settings for the SaltyBet bot.

Settings live in config/settings.json so the interactive CLI and the
background service agree on how the bot should run. Secrets (username /
password) stay in .env and are never written here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "settings.json"

CONF_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass
class Settings:
    # trading
    min_confidence: str = "high"     # low | medium | high
    wager_frac: float = 0.02         # fraction of balance per bet
    max_bet: int = 5000              # hard cap per bet, in Salty Bucks
    interval: int = 5                # poll interval, seconds
    # NOTE: there is deliberately no `execute` field. Real betting is never
    # persisted, so a saved config can never silently re-arm live bets.
    # service
    service_name: str = "saltybot"
    restart_sec: int = 10
    # runtime paths
    log_path: str = "logs/decisions.jsonl"

    def validate(self) -> list[str]:
        errs = []
        if self.min_confidence not in CONF_RANK:
            errs.append(f"min_confidence must be one of {list(CONF_RANK)}")
        if not (0 < self.wager_frac <= 1):
            errs.append("wager_frac must be in (0, 1]")
        if self.max_bet < 1:
            errs.append("max_bet must be >= 1")
        if self.interval < 1:
            errs.append("interval must be >= 1")
        return errs

    def resolved_log_path(self) -> Path:
        p = Path(self.log_path)
        return p if p.is_absolute() else ROOT / p


def load(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults for missing/invalid values."""
    path = path or CONFIG_PATH
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
    known = {f.name: f for f in fields(Settings)}
    kwargs = {}
    for name, f in known.items():
        if name in data:
            val = data[name]
            try:
                if f.type in ("int", int):
                    val = int(val)
                elif f.type in ("float", float):
                    val = float(val)
                elif f.type in ("bool", bool) and isinstance(val, str):
                    val = val.lower() in ("1", "true", "yes", "on")
            except (TypeError, ValueError):
                continue
            kwargs[name] = val
    s = Settings(**kwargs)
    return s


def save(settings: Settings, path: Path | None = None) -> Path:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2) + "\n")
    return path


def env_path() -> Path:
    return ROOT / ".env"


def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or env_path()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def write_env(values: dict[str, str], path: Path | None = None) -> Path:
    path = path or env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
