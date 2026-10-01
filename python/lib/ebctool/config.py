"""Read the ebctool INI file into frozen dataclasses, checking every value as it loads.

Every section and key is optional; anything absent (or left blank) takes the default
below. Relative paths resolve from the current working directory; bin/ebc.sh always
runs from the repo root. Values may carry a trailing "; comment".
"""
from __future__ import annotations

import configparser
import math
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """The config file is absent, unreadable, or holds a bad value; the CLI exits with code 3."""


PARITIES = ("E", "O")
EXAMPLE_PATH = Path("config/config.example.ini")


@dataclass(frozen=True)
class SerialConfig:
    port: str = "auto"
    parity: str = "E"
    baud: int = 9600
    silence_timeout_s: float = 10.0


@dataclass(frozen=True)
class RunConfig:
    log_dir: Path = Path("logs")
    db_path: Path = Path("db/ebctool.db")
    log_frames: bool = False


@dataclass(frozen=True)
class Limits:
    min_current_a: float = 0.5
    max_current_a: float = 20.0
    min_cutoff_v: float = 8.0
    max_cutoff_v: float = 88.0


@dataclass(frozen=True)
class Config:
    path: Path
    serial: SerialConfig
    run: RunConfig
    limits: Limits


def _get(cp: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    return cp.get(section, key, fallback=default).strip() or default


def _int(cp: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    try:
        return cp.getint(section, key, fallback=default)
    except ValueError:
        raise ConfigError("[%s] %s must be an integer" % (section, key))


def _float(cp: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    try:
        value = cp.getfloat(section, key, fallback=default)
    except ValueError:
        raise ConfigError("[%s] %s must be a number" % (section, key))
    if not math.isfinite(value):
        raise ConfigError("[%s] %s must be a number" % (section, key))
    return value


def _bool(cp: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    try:
        return cp.getboolean(section, key, fallback=default)
    except ValueError:
        raise ConfigError("[%s] %s must be true or false" % (section, key))


def _ordered(limits: Limits, low: str, high: str) -> None:
    if getattr(limits, low) > getattr(limits, high):
        raise ConfigError("[limits] %s (%g) must not exceed %s (%g)"
                          % (low, getattr(limits, low), high, getattr(limits, high)))


def load_config(path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError("config file not found: %s" % path)
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    try:
        with open(path, encoding="utf-8") as fh:
            cp.read_file(fh, source=str(path))
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        raise ConfigError("cannot parse %s: %s" % (path, exc))

    defaults = SerialConfig()
    parity = _get(cp, "serial", "parity", defaults.parity).upper()
    if parity not in PARITIES:
        raise ConfigError("[serial] parity must be one of %s, got %r" % (", ".join(PARITIES), parity))
    serial = SerialConfig(
        port=_get(cp, "serial", "port", defaults.port),
        parity=parity,
        baud=_int(cp, "serial", "baud", defaults.baud),
        silence_timeout_s=_float(cp, "serial", "silence_timeout_s", defaults.silence_timeout_s),
    )
    if not serial.silence_timeout_s > 0:
        raise ConfigError("[serial] silence_timeout_s must be positive, got %g" % serial.silence_timeout_s)

    run_defaults = RunConfig()
    run = RunConfig(
        log_dir=Path(_get(cp, "run", "log_dir", str(run_defaults.log_dir))),
        db_path=Path(_get(cp, "run", "db_path", str(run_defaults.db_path))),
        log_frames=_bool(cp, "run", "log_frames", run_defaults.log_frames),
    )

    limit_defaults = Limits()
    limits = Limits(**{name: _float(cp, "limits", name, getattr(limit_defaults, name))
                       for name in ("min_current_a", "max_current_a", "min_cutoff_v", "max_cutoff_v")})
    _ordered(limits, "min_current_a", "max_current_a")
    _ordered(limits, "min_cutoff_v", "max_cutoff_v")

    return Config(path=path, serial=serial, run=run, limits=limits)


def default_config() -> Config:
    """All defaults; used when --config is absent and config/config.ini does not exist."""
    return Config(path=EXAMPLE_PATH, serial=SerialConfig(), run=RunConfig(), limits=Limits())
