"""Hash Commander (hashcommander-pool.duckdns.org) — pool híbrida de Bitcoin Cash.

Modos: PPLNS (1 BCH para quem acha o bloco + o resto dividido) e solo (porta própria).
API pública documentada no site (JSON, CORS aberto, sem chave, atualizada a cada minuto):
  /api/miners  miners[] (janela PPLNS) e solo_miners[], com as máquinas de cada endereço
  /api/pool    formato public-pool: totalHashRate, blocksFound
  /api/stats   formato NOMP: networkDifficulty

Cuidados:
- os endereços vêm mascarados; cada um tem um id = 16 primeiros caracteres do
  SHA-256 do endereço em minúsculas, sem o prefixo "bitcoincash:";
- hashrates em H/s; não há "visto por último" por máquina (usamos o "updated" da API);
- quem saiu da janela PPLNS continua listado com online=false e machines=[].
"""

from __future__ import annotations

import hashlib
import time

import httpx

from ..models import PoolReading, PoolWorker, describe_error, parse_number

BLOCKS_PER_DAY = 144


def address_id(address: str) -> str:
    a = address.strip().lower()
    if a.startswith("bitcoincash:"):
        a = a[len("bitcoincash:"):]
    return hashlib.sha256(a.encode()).hexdigest()[:16]


def _ths(v) -> float:
    return (parse_number(v) or 0.0) / 1e12


def parse(miners: dict, address: str, pool: dict | None = None, stats: dict | None = None,
          now: float | None = None) -> PoolReading:
    now = time.time() if now is None else now
    me = address_id(address)
    seen = parse_number(miners.get("updated")) or now
    r = PoolReading(ok=True, coin="BCH")

    pplns = next((m for m in miners.get("miners") or [] if m.get("id") == me), None)
    solo = next((m for m in miners.get("solo_miners") or [] if m.get("id") == me), None)

    workers: list[PoolWorker] = []
    best = []
    for entry, mode in ((pplns, "PPLNS"), (solo, "solo")):
        if not entry:
            continue
        best.append(parse_number(entry.get("best_share")))
        active = entry.get("online", True) is not False
        for mach in entry.get("machines") or []:
            hr = _ths(mach.get("hashrate"))
            on = active and hr > 0
            workers.append(PoolWorker(name=str(mach.get("name") or "?"), hashrate_ths=hr,
                                      best_diff=parse_number(mach.get("best_share")),
                                      last_seen=seen if on else None, online=on, mode=mode))
    r.workers = sorted(workers, key=lambda w: (w.mode or "", w.name))
    r.hashrate_ths = (_ths(pplns.get("hashrate")) if pplns else 0) + (_ths(solo.get("hashrate")) if solo else 0)
    best = [b for b in best if b is not None]
    r.best_diff = max(best) if best else None

    if pplns:
        r.pplns_percent = parse_number(pplns.get("share_pct"))
        r.payout_estimate = parse_number(pplns.get("receives_bch"))
    r.pool_hashrate_ths = _ths(miners.get("pool_hashrate")) or None

    if pool:
        if r.pool_hashrate_ths is None:
            r.pool_hashrate_ths = _ths(pool.get("totalHashRate")) or None
        found = pool.get("blocksFound")
        r.blocks_found = len(found) if isinstance(found, list) else None

    if stats:
        coin = next(iter((stats.get("pools") or {}).values()), {}) or {}
        r.network_difficulty = parse_number(coin.get("networkDifficulty"))
    if solo:
        r.solo_hashrate_ths = _ths(solo.get("hashrate"))
        if r.network_difficulty:
            net_hs = r.network_difficulty * 2**32 / 600
            r.solo_blocks_per_day = r.solo_hashrate_ths * 1e12 / net_hs * BLOCKS_PER_DAY

    if not workers and not pplns:
        r.error = "endereço não aparece na pool (nem na janela PPLNS nem no solo)"
    return r


async def _get_json(client: httpx.AsyncClient, url: str, timeout: float):
    resp = await client.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


async def fetch(client: httpx.AsyncClient, base_url: str, address: str,
                timeout: float = 15.0) -> PoolReading:
    try:
        miners = await _get_json(client, f"{base_url}/api/miners", timeout)
    except (httpx.HTTPError, ValueError) as e:
        return PoolReading(ok=False, coin="BCH", error=f"Hash Commander indisponível: {describe_error(e)}")
    extras = {}
    for key, path in (("pool", "/api/pool"), ("stats", "/api/stats")):
        try:
            extras[key] = await _get_json(client, base_url + path, timeout)
        except (httpx.HTTPError, ValueError):
            extras[key] = None
    return parse(miners, address, **extras)
