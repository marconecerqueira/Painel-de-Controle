"""AxeOS (Bitaxe) — API HTTP.

Fonte da verdade: https://github.com/bitaxeorg/ESP-Miner/blob/master/main/http_server/openapi.yaml
Unidades: hashRate* em GH/s, temp/vrTemp em °C, power em W.
"""

from __future__ import annotations

import httpx

from ..models import LocalReading, parse_number, describe_error


def _num(v) -> float | None:
    return parse_number(v)


def parse(info: dict) -> LocalReading:
    r = LocalReading(online=True)

    ghs = _num(info.get("hashRate"))
    r.hashrate_ths = ghs / 1000 if ghs is not None else None
    avg = _num(info.get("hashRate_1h"))
    if avg is None:
        avg = _num(info.get("hashRate_10m"))
    r.hashrate_avg_ths = avg / 1000 if avg is not None else None
    exp = _num(info.get("expectedHashrate"))
    r.expected_ths = exp / 1000 if exp is not None else None

    temps = [t for t in (_num(info.get("temp")), _num(info.get("temp2"))) if t is not None and t > 0]
    r.temp_max_c = max(temps) if temps else None
    vr = _num(info.get("vrTemp"))
    r.temp_vr_c = vr if vr is not None and vr > 0 else None

    r.power_w = _num(info.get("power"))
    r.power_measured = True if r.power_w is not None else None
    fan1, fan2 = _num(info.get("fanrpm")), _num(info.get("fan2rpm"))
    r.fan_rpm = ([int(fan1)] if fan1 is not None else []) + ([int(fan2)] if fan2 else [])
    r.fan_pct = _num(info.get("fanspeed"))
    if fan1 == 0 and (r.fan_pct or 0) > 0:
        r.warnings.append("ventoinha parada (0 RPM)")

    up = _num(info.get("uptimeSeconds"))
    r.uptime_s = int(up) if up is not None else None
    acc, rej = _num(info.get("sharesAccepted")), _num(info.get("sharesRejected"))
    r.accepted = int(acc) if acc is not None else None
    r.rejected = int(rej) if rej is not None else None
    # bestDiff: string formatada ("4.29G") até a v2.10, número a partir da v2.11.
    r.best_diff = _num(info.get("bestDiff"))

    url, port = info.get("stratumURL"), info.get("stratumPort")
    r.pool_url = f"{url}:{port}" if url and port else url
    r.pool_user = info.get("stratumUser")

    r.frequency_mhz = _num(info.get("frequency"))
    r.core_voltage_mv = _num(info.get("coreVoltage"))
    r.firmware = info.get("axeOSVersion") or info.get("version")
    model = " ".join(str(x) for x in (info.get("ASICModel"), info.get("boardVersion")) if x)
    r.model = model or None

    if info.get("overheat_mode"):
        r.warnings.append("SUPERAQUECIMENTO: mineração pausada pelo firmware (overheat_mode)")
    if info.get("power_fault"):
        r.warnings.append(f"falha de energia: {info.get('power_fault')}")
    if info.get("hardware_fault"):
        r.warnings.append(f"falha de hardware: {info.get('hardware_fault')}")
    if info.get("isUsingFallbackStratum"):
        r.warnings.append("usando pool reserva (fallback)")
    if info.get("miningPaused"):
        r.warnings.append("mineração pausada")
    return r


async def fetch(client: httpx.AsyncClient, host: str, port: int = 80,
                timeout: float = 5.0) -> LocalReading:
    base = f"http://{host}" if port in (None, 80) else f"http://{host}:{port}"
    try:
        resp = await client.get(f"{base}/api/system/info", timeout=timeout)
        resp.raise_for_status()
        info = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return LocalReading(online=False, error=f"sem resposta da API AxeOS: {describe_error(e)}")
    return parse(info)
