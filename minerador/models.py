"""Modelos normalizados — todas as fontes viram estes formatos."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SUFFIX = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
_DIFF_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)\s*([kKMGTPE]?)\s*$")


def parse_number(value) -> float | None:
    """Aceita número, string numérica ou string com sufixo ('4.29G', '400.96T')."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _DIFF_RE.match(str(value))
    if not m:
        return None
    return float(m.group(1)) * _SUFFIX[m.group(2).upper()]


@dataclass
class LocalReading:
    """Leitura direta da máquina (LuxOS ou AxeOS)."""
    online: bool
    error: str | None = None
    hashrate_ths: float | None = None       # instantâneo
    hashrate_avg_ths: float | None = None   # média (LuxOS: desde o início; AxeOS: 1h)
    expected_ths: float | None = None
    temp_max_c: float | None = None         # chip/placa mais quente
    temp_vr_c: float | None = None          # regulador de tensão (Bitaxe)
    power_w: float | None = None
    power_measured: bool | None = None      # False = estimado pelo firmware
    fan_rpm: list[int] = field(default_factory=list)
    fan_pct: float | None = None
    uptime_s: int | None = None
    accepted: int | None = None
    rejected: int | None = None
    pool_url: str | None = None
    pool_user: str | None = None
    pool_alive: bool | None = None
    best_diff: float | None = None
    firmware: str | None = None
    model: str | None = None
    # controle (Fase 4)
    profile: str | None = None              # LuxOS: perfil de potência atual
    sleeping: bool | None = None            # LuxOS: dormindo (curtail sleep)
    frequency_mhz: float | None = None      # AxeOS
    core_voltage_mv: float | None = None    # AxeOS
    warnings: list[str] = field(default_factory=list)


@dataclass
class PoolWorker:
    name: str
    hashrate_ths: float
    best_diff: float | None
    last_seen: float | None                 # epoch (s)
    online: bool
    mode: str | None = None                 # Hash Commander: "PPLNS" ou "solo"


@dataclass
class PoolReading:
    ok: bool
    error: str | None = None
    hashrate_ths: float | None = None       # o que a pool reporta agora
    hashrate_real_ths: float | None = None  # média dos shares (defisurfers: 2 últimos slots de 10 min)
    workers: list[PoolWorker] = field(default_factory=list)
    best_diff: float | None = None
    balance_sats: int | None = None
    paid_sats: int | None = None
    pplns_percent: float | None = None      # sua fatia da janela PPLNS (defisurfers)
    pool_hashrate_ths: float | None = None
    blocks_found: int | None = None
    coin: str = "BTC"
    payout_estimate: float | None = None    # Hash Commander: quanto recebe (BCH) se a pool achar bloco agora
    solo_hashrate_ths: float | None = None
    solo_blocks_per_day: float | None = None  # chance de bloco solo por dia
    network_difficulty: float | None = None


WORKER_ONLINE_WINDOW_S = 600  # visto há menos de 10 min = online


def describe_error(e: BaseException) -> str:
    """Texto legível para uma exceção (várias, como timeouts, têm str() vazio)."""
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__
