"""Carrega e valida o config.toml."""

from __future__ import annotations

import re
import tomllib
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from .alerts import AlertSettings

MINER_TYPES = {"luxos", "axeos", "pool_only", "rental"}
POOL_TYPES = {"defisurfers", "parasite", "hashcommander"}
DEFAULT_PORTS = {"luxos": 4028, "axeos": 80}


class ConfigError(ValueError):
    pass


@dataclass
class MinerConfig:
    id: str
    name: str
    type: str
    host: str | None = None
    port: int | None = None
    pool: str | None = None
    worker: str | None = None
    expected_ths: float | None = None
    temp_max_c: float | None = None
    vr_temp_max_c: float | None = None
    # aluguel de hashrate (Fase 3): quanto custou, quanto dura e quando começou
    cost_btc: float | None = None
    rental_hours: float | None = None
    rental_start: float | None = None       # epoch; None = detectado pelo painel
    # máquina própria (Fase 3): consumo nominal (vale quando a máquina não informa) ou taxa fixa
    power_w: float | None = None
    energy_eur_month: float | None = None


@dataclass
class PoolConfig:
    id: str
    type: str
    address: str
    base_url: str
    fee_pct: float | None = None            # taxa da pool; None = a da cotação (0 no BTC)


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: str = "minerador.db"
    local_interval: float = 15
    pool_interval: float = 60
    retention_days: float = 30
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    alerts: AlertSettings = field(default_factory=AlertSettings)
    miners: list[MinerConfig] = field(default_factory=list)
    pools: dict[str, PoolConfig] = field(default_factory=dict)
    mempool_url: str = "https://mempool.space"
    block_pools: list[str] = field(default_factory=list)  # slugs extras no filtro "Minhas pools"
    energy_eur_kwh: float | None = None     # [finance]: tarifa de energia
    location: dict | None = None            # [location]: local fixo do cartão de tempo (senão, o do aparelho)
    leda: dict | None = None                # [leda]: assistente de voz (Gemini); None = desligada


def _positive(m: dict, key: str, mid: str, where: str | None = None) -> float | None:
    v = m.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ConfigError(f"{where or f'máquina {mid!r}'}: {key} deve ser um número maior que zero "
                          "(use ponto como separador decimal, ex.: 0.14)")
    return float(v)


def _parse_start(v, mid: str) -> float | None:
    """Aceita "2026-11-16 23:39" (hora local) ou data/hora TOML sem aspas."""
    if v is None or v == "":
        return None
    try:
        dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).strip())
    except ValueError:
        raise ConfigError(f'máquina \'{mid}\': rental_start inválido, use "AAAA-MM-DD HH:MM"') from None
    return dt.timestamp()   # sem fuso = hora local do servidor


def _location(raw: dict) -> dict | None:
    """[location]: name, lat, lon obrigatórios; country e country_code opcionais."""
    loc = raw.get("location")
    if not loc:
        return None
    if not isinstance(loc, dict):
        raise ConfigError("[location] deve ser uma seção com name, lat e lon")
    name = str(loc.get("name") or "").strip()
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError("[location] precisa de lat e lon numéricos (ex.: lat = 41.1496, lon = -8.611)") from None
    if not name:
        raise ConfigError('[location] precisa de name (ex.: name = "Porto")')
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ConfigError("[location] lat deve estar entre -90 e 90, e lon entre -180 e 180")
    cc = str(loc.get("country_code") or "").strip().upper()
    if cc and not re.fullmatch(r"[A-Z]{2}", cc):
        raise ConfigError('[location] country_code deve ter 2 letras (ex.: "PT")')
    return {"name": name, "country": str(loc.get("country") or "").strip(), "country_code": cc or None,
            "lat": lat, "lon": lon}


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"arquivo de configuração não encontrado: {path}")
    try:
        # utf-8-sig: o Bloco de Notas do Windows pode salvar com BOM, que o tomllib recusa
        raw = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise ConfigError(f"{path.name}: salve o arquivo como UTF-8") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} com erro de digitação: {e}") from None
    return parse_config(raw, base_dir=path.parent)


def parse_config(raw: dict, base_dir: Path | None = None) -> Config:
    server = raw.get("server", {})
    intervals = raw.get("intervals", {})

    pools: dict[str, PoolConfig] = {}
    for pid, p in raw.get("pools", {}).items():
        ptype = p.get("type", pid)
        if ptype not in POOL_TYPES:
            raise ConfigError(f"pool '{pid}': type deve ser um de {sorted(POOL_TYPES)}")
        if not p.get("address"):
            raise ConfigError(f"pool '{pid}': falta 'address'")
        if not p.get("base_url"):
            raise ConfigError(f"pool '{pid}': falta 'base_url'")
        fee = p.get("fee_pct")
        if fee is not None and (isinstance(fee, bool) or not isinstance(fee, (int, float))
                                or not 0 <= fee < 100):
            raise ConfigError(f"pool '{pid}': fee_pct deve ser um número de 0 a 100")
        pools[pid] = PoolConfig(id=pid, type=ptype, address=p["address"].strip(),
                                base_url=p["base_url"].rstrip("/"),
                                fee_pct=float(fee) if fee is not None else None)

    miners: list[MinerConfig] = []
    seen: set[str] = set()
    for m in raw.get("miners", []):
        mid = m.get("id")
        if not mid:
            raise ConfigError("toda máquina em [[miners]] precisa de 'id'")
        if mid in seen:
            raise ConfigError(f"id de máquina repetido: {mid}")
        seen.add(mid)
        mtype = m.get("type")
        if mtype not in MINER_TYPES:
            raise ConfigError(f"máquina '{mid}': type deve ser um de {sorted(MINER_TYPES)}")
        if mtype in ("luxos", "axeos") and not m.get("host"):
            raise ConfigError(f"máquina '{mid}': falta 'host'")
        if m.get("pool") and m["pool"] not in pools:
            raise ConfigError(f"máquina '{mid}': pool '{m['pool']}' não existe em [pools]")
        if mtype in ("pool_only", "rental") and not (m.get("pool") and m.get("worker")):
            raise ConfigError(f"máquina '{mid}': type {mtype} exige 'pool' e 'worker'")
        miners.append(MinerConfig(
            id=mid,
            name=m.get("name", mid),
            type=mtype,
            host=m.get("host"),
            port=int(m.get("port") or DEFAULT_PORTS.get(mtype, 0)) or None,
            pool=m.get("pool"),
            worker=m.get("worker"),
            expected_ths=m.get("expected_ths"),
            temp_max_c=m.get("temp_max_c"),
            vr_temp_max_c=m.get("vr_temp_max_c"),
            cost_btc=_positive(m, "cost_btc", mid),
            rental_hours=_positive(m, "rental_hours", mid),
            rental_start=_parse_start(m.get("rental_start"), mid),
            power_w=_positive(m, "power_w", mid),
            energy_eur_month=_positive(m, "energy_eur_month", mid),
        ))

    db_path = server.get("db_path", "minerador.db")
    if base_dir is not None and not Path(db_path).is_absolute():
        db_path = str(base_dir / db_path)

    tg = raw.get("telegram", {})
    token = str(tg.get("bot_token") or "").strip() or None
    chat_id = str(tg.get("chat_id") or "").strip() or None
    if bool(token) != bool(chat_id):
        raise ConfigError("[telegram]: preencha bot_token E chat_id (ou deixe os dois vazios)")

    a = raw.get("alerts", {})
    alerts = AlertSettings()
    for key, attr in (("offline_min", "offline_s"), ("hashrate_min", "hashrate_s"),
                      ("temp_min", "temp_s"), ("pool_min", "pool_s"), ("rental_min", "rental_s")):
        if key in a:
            setattr(alerts, attr, float(a[key]) * 60)
    if "low_hashrate_pct" in a:
        alerts.low_hashrate_ratio = float(a["low_hashrate_pct"]) / 100

    blocks = raw.get("blocks", {})
    mempool_url = str(blocks.get("base_url") or "https://mempool.space").rstrip("/")
    block_pools = blocks.get("my_pools", [])
    if not isinstance(block_pools, list) or not all(
            isinstance(x, str) and re.fullmatch(r"[a-z0-9-]{1,40}", x) for x in block_pools):
        raise ConfigError('[blocks] my_pools deve ser uma lista de slugs do mempool, ex.: ["parasite"]')

    energy = _positive(raw.get("finance", {}), "energy_eur_kwh", "", "[finance]")

    ld = raw.get("leda", {})
    if not isinstance(ld, dict):
        raise ConfigError("[leda] deve ser uma seção com api_key")
    leda_key = str(ld.get("api_key") or "").strip()
    leda = {"api_key": leda_key, "voice": str(ld.get("voice") or "Leda"),
            "text_model": str(ld.get("text_model") or "gemini-flash-lite-latest"),
            "tts_model": str(ld.get("tts_model") or "gemini-3.8-flash-tts")} if leda_key else None

    return Config(
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 8080)),
        db_path=db_path,
        local_interval=float(intervals.get("local", 15)),
        pool_interval=float(intervals.get("pools", 60)),
        retention_days=float(server.get("retention_days", 30)),
        telegram_token=token,
        telegram_chat_id=chat_id,
        alerts=alerts,
        miners=miners,
        pools=pools,
        mempool_url=mempool_url,
        block_pools=block_pools,
        energy_eur_kwh=energy,
        location=_location(raw),
        leda=leda,
    )
