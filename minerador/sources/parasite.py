"""Parasite Pool (parasite.space) — API pública e NÃO documentada (extraída do frontend).

Cuidados:
- tipos misturados: workerData[].hashrate/bestDifficulty/lastSubmission são strings;
  bestDifficulty de topo vem formatado ("400.96T");
- 404 = endereço não encontrado / inativo;
- limite ~200 req/min.
"""

from __future__ import annotations

import time

import httpx

from ..models import WORKER_ONLINE_WINDOW_S, PoolReading, PoolWorker, parse_number, describe_error


def parse(user: dict, now: float | None = None) -> PoolReading:
    now = time.time() if now is None else now
    r = PoolReading(ok=True)
    hr = parse_number(user.get("hashrate"))
    r.hashrate_ths = hr / 1e12 if hr is not None else None
    r.best_diff = parse_number(user.get("bestDifficulty"))

    workers = []
    for w in user.get("workerData") or []:
        name = str(w.get("name") or w.get("id") or "?")
        whr = (parse_number(w.get("hashrate")) or 0) / 1e12
        seen = parse_number(w.get("lastSubmission"))
        online = whr > 0 and seen is not None and now - seen < WORKER_ONLINE_WINDOW_S
        workers.append(PoolWorker(name=name, hashrate_ths=whr,
                                  best_diff=parse_number(w.get("bestDifficulty")),
                                  last_seen=seen, online=online))
    r.workers = sorted(workers, key=lambda w: w.name)
    return r


async def fetch(client: httpx.AsyncClient, base_url: str, address: str,
                timeout: float = 15.0) -> PoolReading:
    try:
        resp = await client.get(f"{base_url}/api/user/{address}", timeout=timeout)
        if resp.status_code == 404:
            return PoolReading(ok=False, error=(
                "endereço não encontrado na Parasite — se já está minerando, "
                f"abra {base_url}/user/{address} uma vez no navegador"))
        resp.raise_for_status()
        return parse(resp.json())
    except (httpx.HTTPError, ValueError) as e:
        return PoolReading(ok=False, error=f"Parasite indisponível: {describe_error(e)}")
