"""mempool.space: blocos recentes da rede e blocos por pool. API pública, sem autenticação.

Cuidados:
- a pool de cada bloco vem em extras.pool (slug); a defisurfers NÃO é identificada pelo
  mempool, então os blocos dela vêm da própria pool (/api/pool -> blocksFound[].height);
- /api/v1/mining/pool/{slug}/blocks devolve até 100 blocos, do mais novo para o mais velho;
- /api/block-height/{h} devolve o hash como texto puro.
"""

from __future__ import annotations

import httpx

DEFAULT_URL = "https://mempool.space"

# Pools solo e pequenas: onde um Bitaxe/NerdMiner ou uma S19 isolada pode achar um bloco.
# "unknown" fica de fora: domina a lista e não é necessariamente solo (está no seletor de pool).
SOLO_POOLS = ["solock", "publicpool", "parasite", "braiinssolo", "solopoolcom", "futurebitapollosolo"]

# tipo de pool do config -> slug no mempool (defisurfers não tem)
POOL_TYPE_SLUG = {"parasite": "parasite"}


def parse_block(b: dict) -> dict:
    ex = b.get("extras") or {}
    pool = ex.get("pool") or {}
    fee_range = ex.get("feeRange") if isinstance(ex.get("feeRange"), list) else []
    return {
        "height": b.get("height"),
        "hash": b.get("id"),
        "ts": b.get("timestamp"),
        "pool_name": pool.get("name") or "Desconhecida",
        "pool_slug": pool.get("slug") or "unknown",
        "reward_sats": ex.get("reward"),
        "fees_sats": ex.get("totalFees"),
        "tx_count": b.get("tx_count"),
        "size": b.get("size"),
        "weight": b.get("weight"),
        "median_fee": ex.get("medianFee"),
        "fee_min": fee_range[0] if fee_range else None,
        "fee_max": fee_range[-1] if fee_range else None,
        "coinbase_addresses": ex.get("coinbaseAddresses") or
                              ([ex["coinbaseAddress"]] if ex.get("coinbaseAddress") else []),
    }


async def _get(client: httpx.AsyncClient, url: str, timeout: float):
    resp = await client.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


async def recent_blocks(client: httpx.AsyncClient, base_url: str, timeout: float = 15.0) -> list[dict]:
    """Os 15 blocos mais recentes da rede."""
    data = (await _get(client, f"{base_url}/api/v1/blocks", timeout)).json()
    return [parse_block(b) for b in data if isinstance(b, dict)]


async def pool_blocks(client: httpx.AsyncClient, base_url: str, slug: str,
                      timeout: float = 15.0) -> list[dict]:
    """Blocos mais recentes de uma pool (até 100)."""
    data = (await _get(client, f"{base_url}/api/v1/mining/pool/{slug}/blocks", timeout)).json()
    return [parse_block(b) for b in data if isinstance(b, dict)]


async def block_at(client: httpx.AsyncClient, base_url: str, height: int,
                   timeout: float = 15.0) -> dict:
    block_hash = (await _get(client, f"{base_url}/api/block-height/{int(height)}", timeout)).text.strip()
    return parse_block((await _get(client, f"{base_url}/api/v1/block/{block_hash}", timeout)).json())


async def pools(client: httpx.AsyncClient, base_url: str, period: str = "1y",
                timeout: float = 15.0) -> list[dict]:
    """Pools com bloco no período, da maior para a menor."""
    data = (await _get(client, f"{base_url}/api/v1/mining/pools/{period}", timeout)).json()
    out = [{"slug": p.get("slug"), "name": p.get("name"), "blocks": p.get("blockCount")}
           for p in data.get("pools", []) if p.get("slug")]
    return sorted(out, key=lambda p: -(p["blocks"] or 0))
