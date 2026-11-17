"""Blocos encontrados (mempool.space + defisurfers), com filtros e cache.

Filtros (parâmetro view):
  rede          últimos 15 blocos da rede
  minhas        blocos das pools onde as suas máquinas mineram
  solo          blocos de pools solo/pequenas (Solo CK, Public Pool, Parasite…)
  pool:<slug>   blocos de uma pool qualquer conhecida pelo mempool
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from .config import Config
from .models import describe_error
from .models import parse_number
from .sources import hashcommander, mempool

log = logging.getLogger("minerador.blocks")

LIMIT = 25
BCH_EXPLORER = "https://blockchair.com/bitcoin-cash/block/"
TTL_RECENT_S = 60
TTL_POOL_S = 300
TTL_POOL_LIST_S = 6 * 3600
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")


class BlockFeed:
    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.base = cfg.mempool_url
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, object]] = {}
        self._by_height: dict[int, dict] = {}          # blocos da defisurfers já resolvidos
        self.my_addresses = {p.address for p in cfg.pools.values()}
        slugs = [mempool.POOL_TYPE_SLUG[p.type] for p in cfg.pools.values() if p.type in mempool.POOL_TYPE_SLUG]
        self.my_slugs = list(dict.fromkeys(slugs + cfg.block_pools))
        self.defi_pools = [p for p in cfg.pools.values() if p.type == "defisurfers"]
        self.hc_pools = [p for p in cfg.pools.values() if p.type == "hashcommander"]
        self.my_ids = {hashcommander.address_id(p.address) for p in self.hc_pools}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"User-Agent": "minerador/0.1"},
                                             follow_redirects=True, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _cached(self, key: str, ttl: float, fn):
        """Devolve (dados, erro). Em falha, usa a última resposta boa (se houver)."""
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1], None
        try:
            data = await fn()
        except (httpx.HTTPError, ValueError) as e:
            msg = describe_error(e)
            log.warning("blocos: %s falhou: %s", key, msg)
            if hit:
                return hit[1], f"dados de {time.strftime('%H:%M', time.localtime(hit[0]))} (falha: {msg})"
            return None, msg
        self._cache[key] = (time.time(), data)
        return data, None

    # ------------------------------------------------------------------ fontes
    async def _defi_found(self) -> tuple[list[dict], list[str], list[str]]:
        """Blocos achados pelas pools defisurfers do config (a própria pool informa a altura)."""
        blocks, notes, errors = [], [], []
        for p in self.defi_pools:
            info, err = await self._cached(f"defi:{p.id}", TTL_POOL_S,
                                           lambda p=p: self._get_json(f"{p.base_url}/api/pool"))
            if err:
                errors.append(f"{p.id}: {err}")
            found = (info or {}).get("blocksFound") if isinstance(info, dict) else None
            if not isinstance(found, list):
                continue
            notes.append(f"pool.defisurfers.xyz: {len(found)} bloco{'s' if len(found) != 1 else ''} "
                         f"encontrado{'s' if len(found) != 1 else ''} até agora" if found
                         else "pool.defisurfers.xyz: ainda não encontrou nenhum bloco")
            entries = sorted((f for f in found if isinstance(f, dict) and f.get("height")),
                             key=lambda f: -int(f["height"]))[:LIMIT]
            for f in entries:
                h = int(f["height"])
                if h not in self._by_height:
                    try:
                        self._by_height[h] = await mempool.block_at(self.client, self.base, h)
                    except (httpx.HTTPError, ValueError) as e:
                        self._by_height[h] = {"height": h, "hash": None, "ts": None, "pool_slug": "unknown",
                                              "reward_sats": None, "fees_sats": None, "tx_count": None,
                                              "size": None, "weight": None, "median_fee": None,
                                              "fee_min": None, "fee_max": None,
                                              "coinbase_addresses": [], "_partial": True}
                        log.warning("bloco %s da defisurfers sem detalhes: %s", h, describe_error(e))
                b = dict(self._by_height[h], pool_name="defisurfers", pool_slug="defisurfers")
                b["found_by_me"] = f.get("minerAddress") in self.my_addresses
                b["worker"] = f.get("worker")
                blocks.append(b)
            if any(b.get("_partial") for b in self._by_height.values()):
                # tenta de novo na próxima consulta
                self._by_height = {h: b for h, b in self._by_height.items() if not b.get("_partial")}
        return blocks, notes, errors

    async def _hc_found(self) -> tuple[list[dict], list[str], list[str]]:
        """Blocos de Bitcoin Cash achados pela Hash Commander (API da própria pool)."""
        blocks, notes, errors = [], [], []
        for p in self.hc_pools:
            data, err = await self._cached(f"hc:{p.id}", TTL_POOL_S,
                                           lambda p=p: self._get_json(f"{p.base_url}/api/blocks"))
            if err:
                errors.append(f"{p.id}: {err}")
            found = (data or {}).get("blocks") if isinstance(data, dict) else None
            if not isinstance(found, list):
                continue
            n = len(found)
            notes.append(f"Hash Commander (BCH): {n} bloco{'s' if n != 1 else ''} encontrado{'s' if n != 1 else ''}"
                         if n else "Hash Commander (BCH): ainda não encontrou nenhum bloco")
            for f in found:
                if not isinstance(f, dict):
                    continue
                height = parse_number(f.get("height", f.get("altura")))
                if height is None:
                    continue
                reward = parse_number(f.get("reward_bch", f.get("recompensa_bch", f.get("reward"))))
                finder = f.get("finder") if isinstance(f.get("finder"), dict) else {}
                worker = f.get("worker") or f.get("machine") or f.get("maquina") or finder.get("name")
                finder_id = f.get("finder_id") or f.get("id_finder") or finder.get("id")
                blocks.append({
                    "height": int(height), "hash": f.get("hash"),
                    "ts": parse_number(f.get("ts", f.get("time", f.get("timestamp")))),
                    "pool_name": "Hash Commander", "pool_slug": "hashcommander", "coin": "BCH",
                    "explorer_url": BCH_EXPLORER + str(int(height)),
                    "reward_sats": round(reward * 1e8) if reward is not None and reward < 100 else None,
                    "fees_sats": None, "tx_count": parse_number(f.get("tx_count")),
                    "size": parse_number(f.get("size")), "weight": None,
                    "median_fee": None, "fee_min": None, "fee_max": None,
                    "worker": worker, "found_by_me": finder_id in self.my_ids,
                    "coinbase_addresses": [],
                })
        return blocks, notes, errors

    async def _get_json(self, url: str):
        resp = await self.client.get(url, timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    async def _pool(self, slug: str):
        return await self._cached(f"pool:{slug}", TTL_POOL_S,
                                  lambda: mempool.pool_blocks(self.client, self.base, slug))

    async def _merge_pools(self, slugs: list[str]) -> tuple[list[dict], list[str]]:
        results = await asyncio.gather(*(self._pool(s) for s in slugs))
        blocks, errors = [], []
        for slug, (data, err) in zip(slugs, results):
            if err:
                errors.append(f"{slug}: {err}")
            blocks.extend(data or [])
        return blocks, errors

    # ------------------------------------------------------------------ API
    def _mark(self, blocks: list[dict], defi_heights: set[int]) -> list[dict]:
        # BTC e BCH têm alturas parecidas: a chave é (moeda, altura); a ordem é pela data
        out, seen = [], set()
        for b in sorted(blocks, key=lambda b: (-(b.get("ts") or 0), -(b.get("height") or 0))):
            coin = b.get("coin") or "BTC"
            if (coin, b.get("height")) in seen:
                continue
            seen.add((coin, b.get("height")))
            b = dict(b, coin=coin)
            b["mine"] = (b.get("pool_slug") in self.my_slugs or b.get("pool_slug") == "hashcommander"
                         or (coin == "BTC" and b.get("height") in defi_heights))
            b["paid_me"] = bool(self.my_addresses & set(b.pop("coinbase_addresses", None) or []))
            b.pop("_partial", None)
            out.append(b)
        return out[:LIMIT]

    async def get(self, view: str) -> dict:
        notes: list[str] = []
        errors: list[str] = []
        defi_blocks, defi_notes, defi_errors = await self._defi_found()
        defi_heights = {b["height"] for b in defi_blocks}

        if view == "rede":
            data, err = await self._cached("recent", TTL_RECENT_S,
                                           lambda: mempool.recent_blocks(self.client, self.base))
            blocks = data or []
            if err:
                errors.append(err)
            # na rede, o bloco da defisurfers aparece com o nome certo
            by_h = {b["height"]: b for b in defi_blocks}
            blocks = [by_h.get(b["height"], b) for b in blocks]
        elif view == "minhas":
            blocks, errors = await self._merge_pools(self.my_slugs)
            hc_blocks, hc_notes, hc_errors = await self._hc_found()
            blocks += defi_blocks + hc_blocks
            notes += defi_notes + hc_notes
            errors += defi_errors + hc_errors
            for slug in self.my_slugs:
                n = sum(1 for b in blocks if b.get("pool_slug") == slug)
                if n == 0 and not any(e.startswith(slug + ":") for e in errors):
                    notes.append(f"{slug}: nenhum bloco recente no mempool")
            if not self.my_slugs and not self.defi_pools and not self.hc_pools:
                notes.append("Nenhuma pool configurada.")
        elif view == "solo":
            blocks, errors = await self._merge_pools(mempool.SOLO_POOLS)
            blocks += defi_blocks
        elif view.startswith("pool:") and SLUG_RE.match(view[5:]):
            slug = view[5:]
            if slug == "defisurfers":
                blocks, notes, errors = defi_blocks, defi_notes, defi_errors
            elif slug == "hashcommander":
                blocks, notes, errors = await self._hc_found()
            else:
                data, err = await self._pool(slug)
                blocks = data or []
                if err:
                    errors.append(err)
        else:
            raise ValueError(f"filtro inválido: {view}")

        return {"view": view, "blocks": self._mark(blocks, defi_heights), "notes": notes,
                "error": "; ".join(errors) or None, "mempool_url": self.base,
                "my_pools": self.my_slugs + (["defisurfers"] if self.defi_pools else [])
                            + (["hashcommander"] if self.hc_pools else []),
                "generated_at": time.time()}

    async def pool_list(self) -> dict:
        data, err = await self._cached("pools", TTL_POOL_LIST_S,
                                       lambda: mempool.pools(self.client, self.base, "1y"))
        items = list(data or [])
        if self.defi_pools and not any(p["slug"] == "defisurfers" for p in items):
            items.append({"slug": "defisurfers", "name": "defisurfers (dados da pool)", "blocks": None})
        if self.hc_pools:
            items.append({"slug": "hashcommander", "name": "Hash Commander (BCH)", "blocks": None})
        return {"pools": items, "error": err}
