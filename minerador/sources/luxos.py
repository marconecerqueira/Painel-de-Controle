"""LuxOS (Antminer) — API cgminer-compatível na porta TCP 4028.

Docs: https://docs.luxor.tech/firmware/api/intro
Cada requisição abre uma conexão, envia {"command","parameter"} e lê até o EOF.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from ..models import LocalReading, describe_error


class LuxOSError(RuntimeError):
    pass


async def command(host: str, port: int, cmd: str, parameter: str | None = None,
                  timeout: float = 5.0) -> dict:
    payload: dict = {"command": cmd}
    if parameter is not None:
        payload["parameter"] = parameter
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        writer.write(json.dumps(payload).encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(-1), timeout)
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
    text = data.decode("utf-8", errors="replace").strip("\x00 \r\n\t")
    if not text:
        raise LuxOSError(f"{cmd}: resposta vazia")
    try:
        resp = json.loads(text)
    except json.JSONDecodeError as e:
        raise LuxOSError(f"{cmd}: JSON inválido ({e})") from e
    status = (resp.get("STATUS") or [{}])[0]
    # Sucesso é STATUS == "S" ("I" = informativo também é ok). Não usar Code (muda entre versões).
    if status.get("STATUS") not in ("S", "I"):
        raise LuxOSError(f"{cmd}: {status.get('Msg') or status}")
    return resp


def _first(resp: dict | None, key: str) -> dict:
    if not resp:
        return {}
    items = resp.get(key) or []
    return items[0] if items and isinstance(items[0], dict) else {}


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(summary: dict, pools: dict | None = None, power: dict | None = None,
          temps: dict | None = None, fans: dict | None = None,
          version: dict | None = None, config: dict | None = None) -> LocalReading:
    """Converte as respostas brutas do LuxOS numa LocalReading. Funções auxiliares opcionais."""
    s = _first(summary, "SUMMARY")
    r = LocalReading(online=True)

    ghs_5s = _num(s.get("GHS 5s"))
    ghs_av = _num(s.get("GHS av"))
    r.hashrate_ths = ghs_5s / 1000 if ghs_5s is not None else None
    r.hashrate_avg_ths = ghs_av / 1000 if ghs_av is not None else None
    elapsed = _num(s.get("Elapsed"))
    r.uptime_s = int(elapsed) if elapsed is not None else None
    acc, rej = _num(s.get("Accepted")), _num(s.get("Rejected"))
    r.accepted = int(acc) if acc is not None else None
    r.rejected = int(rej) if rej is not None else None
    r.best_diff = _num(s.get("Best Share"))

    hw = _num(s.get("Device Hardware%"))
    if hw is not None and hw > 5:
        r.warnings.append(f"erros de hardware altos ({hw:.1f}%)")

    if power:
        p = _first(power, "POWER")
        r.power_w = _num(p.get("Watts"))
        if "PSU" in p:
            r.power_measured = bool(p.get("PSU"))

    if temps:
        values: list[float] = []
        for entry in temps.get("TEMPS") or []:
            if not isinstance(entry, dict):
                continue
            for k, v in entry.items():
                # As chaves variam por modelo (TopLeft, BottomRight, ...); ignora identificadores.
                if "ID" in k.upper() or k.upper() == "TEMP":
                    continue
                n = _num(v)
                if n is not None and 0 < n < 150:
                    values.append(n)
        if values:
            r.temp_max_c = max(values)

    if fans:
        rpms, pcts = [], []
        for f in fans.get("FANS") or []:
            rpm = _num(f.get("RPM"))
            if rpm is not None:
                rpms.append(int(rpm))
            pct = _num(f.get("Speed"))
            if pct is not None:
                pcts.append(pct)
        r.fan_rpm = rpms
        r.fan_pct = max(pcts) if pcts else None
        if rpms and min(rpms) == 0:
            r.warnings.append("ventoinha parada (0 RPM)")

    if pools:
        plist = [p for p in (pools.get("POOLS") or []) if isinstance(p, dict)]
        active = next((p for p in plist if p.get("Stratum Active")), None) \
            or next((p for p in plist if p.get("Status") == "Alive"), None) \
            or (plist[0] if plist else None)
        if active:
            r.pool_url = active.get("URL")
            r.pool_user = active.get("User")
            r.pool_alive = active.get("Status") == "Alive"
            if not r.pool_alive:
                r.warnings.append(f"pool {active.get('Status', '?')}")

    if config:
        c = _first(config, "CONFIG")
        r.profile = c.get("Profile") or None
        if "CurtailMode" in c:
            r.sleeping = str(c.get("CurtailMode") or "").lower() == "sleep"

    if version:
        v = _first(version, "VERSION")
        r.firmware = v.get("LUXminer") or v.get("Miner") or v.get("CGMiner")
        r.model = v.get("Type")

    return r


async def fetch(host: str, port: int = 4028, timeout: float = 5.0) -> LocalReading:
    try:
        summary = await command(host, port, "summary", timeout=timeout)
    except (OSError, asyncio.TimeoutError, LuxOSError) as e:
        return LocalReading(online=False, error=f"sem resposta da API LuxOS: {describe_error(e)}")

    extras: dict[str, dict | None] = {}
    warnings: list[str] = []
    for name in ("pools", "power", "temps", "fans", "version", "config"):
        try:
            extras[name] = await command(host, port, name, timeout=timeout)
        except (OSError, asyncio.TimeoutError, LuxOSError) as e:
            extras[name] = None
            warnings.append(f"falha ao ler '{name}': {describe_error(e)}")

    reading = parse(summary, **extras)
    reading.warnings = warnings + reading.warnings
    return reading
