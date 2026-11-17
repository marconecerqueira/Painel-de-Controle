"""pool.defisurfers.xyz — fork de public-pool com PPLNS. API pública, sem autenticação.

Cuidados:
- endereço inválido devolve 200 com zeros (não 404);
- workers desconectados somem da lista;
- workers[].bestDifficulty é string; hashRate em H/s.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx

from ..models import WORKER_ONLINE_WINDOW_S, PoolReading, PoolWorker, parse_number, describe_error


def _iso_to_epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse(client: dict, chart: list | None = None, pplns: dict | None = None,
          pool: dict | None = None, now: float | None = None) -> PoolReading:
    now = time.time() if now is None else now
    r = PoolReading(ok=True)

    total = parse_number(client.get("totalHashrate"))
    r.hashrate_ths = total / 1e12 if total is not None else None
    r.best_diff = parse_number(client.get("bestDifficulty"))

    # Um mesmo worker pode ter várias sessões (reconexões): agrega por nome.
    by_name: dict[str, PoolWorker] = {}
    for w in client.get("workers") or []:
        name = str(w.get("name") or "?")
        hr = (parse_number(w.get("hashRate")) or 0) / 1e12
        bd = parse_number(w.get("bestDifficulty"))
        seen = _iso_to_epoch(w.get("lastSeen"))
        cur = by_name.get(name)
        if cur is None:
            by_name[name] = PoolWorker(name=name, hashrate_ths=hr, best_diff=bd, last_seen=seen, online=False)
        else:
            cur.hashrate_ths += hr
            if bd is not None and (cur.best_diff is None or bd > cur.best_diff):
                cur.best_diff = bd
            if seen is not None and (cur.last_seen is None or seen > cur.last_seen):
                cur.last_seen = seen
    for w in by_name.values():
        w.online = w.last_seen is not None and now - w.last_seen < WORKER_ONLINE_WINDOW_S
    r.workers = sorted(by_name.values(), key=lambda w: w.name)

    if chart:
        # O próprio site usa a média dos 2 últimos slots de 10 min como hashrate "real".
        pts = [parse_number(p.get("data")) for p in chart[-2:] if isinstance(p, dict)]
        pts = [p for p in pts if p is not None]
        if pts:
            r.hashrate_real_ths = sum(pts) / len(pts) / 1e12

    if pplns:
        bal, paid = parse_number(pplns.get("balanceSats")), parse_number(pplns.get("totalPaidSats"))
        r.balance_sats = int(bal) if bal is not None else None
        r.paid_sats = int(paid) if paid is not None else None
        r.pplns_percent = parse_number(pplns.get("currentWindowPercent"))

    if pool:
        ph = parse_number(pool.get("totalHashRate"))
        r.pool_hashrate_ths = ph / 1e12 if ph is not None else None
        blocks = pool.get("blocksFound")
        r.blocks_found = len(blocks) if isinstance(blocks, list) else None

    return r


async def _get_json(client: httpx.AsyncClient, url: str, timeout: float):
    resp = await client.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


async def fetch(client: httpx.AsyncClient, base_url: str, address: str,
                timeout: float = 15.0) -> PoolReading:
    try:
        data = await _get_json(client, f"{base_url}/api/client/{address}", timeout)
    except (httpx.HTTPError, ValueError) as e:
        return PoolReading(ok=False, error=f"defisurfers indisponível: {describe_error(e)}")

    extras = {}
    for key, path in (("chart", f"/api/client/{address}/chart?range=1d"),
                      ("pplns", f"/api/pplns/{address}"),
                      ("pool", "/api/pool")):
        try:
            extras[key] = await _get_json(client, base_url + path, timeout)
        except (httpx.HTTPError, ValueError):
            extras[key] = None
    reading = parse(data, **extras)
    if not reading.workers and not reading.hashrate_ths:
        reading.error = "nenhum worker ativo neste endereço (confira o endereço no config)"
    return reading


async def fetch_chart(client: httpx.AsyncClient, base_url: str, address: str, range_: str,
                      timeout: float = 25.0) -> list[list[float]]:
    """Série de hashrate do endereço (slots de 10 min). range_: 1d | 3d | 7d | 1m.

    Retorna [[epoch, TH/s], ...]. Levanta httpx.HTTPError em falha.
    """
    data = await _get_json(client, f"{base_url}/api/client/{address}/chart?range={range_}", timeout)
    out = []
    for p in data if isinstance(data, list) else []:
        ts, v = _iso_to_epoch(p.get("label")), parse_number(p.get("data"))
        if ts is not None and v is not None:
            out.append([ts, v / 1e12])
    return out
