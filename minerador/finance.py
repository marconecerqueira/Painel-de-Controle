"""Financeiro dos aluguéis de hashrate (Fase 3, parte 1).

Para cada máquina type="rental" com cost_btc e rental_hours no config:
  - período do aluguel (início do config ou detectado pelas leituras da pool);
  - hashrate médio entregue (leituras gravadas pelo painel);
  - valor esperado: hashrate / hashrate da rede × blocos por dia × recompensa × (1 − taxa);
  - no solo, a chance de achar pelo menos um bloco no período;
  - preço pago por TH/dia, comparado com o "hashprice" do BTC (quanto 1 TH/s rende por dia).

Cotações: BTC e dificuldade pelo mempool.space; BCH, recompensa e taxas pela própria
Hash Commander (estado.json, documentado na página da API da pool).
"""

from __future__ import annotations

import logging
import math
import time

import httpx

from .models import describe_error, parse_number

log = logging.getLogger("minerador.finance")

MARKET_TTL_S = 600
BLOCKS_PER_DAY = 144
TWO_32 = 2 ** 32


def network_ths(difficulty: float) -> float:
    """Hashrate da rede (TH/s) estimado pela dificuldade (blocos a cada 600 s)."""
    return difficulty * TWO_32 / 600 / 1e12


def hashprice(difficulty: float, reward: float) -> float:
    """Quanto 1 TH/s rende por dia, na moeda da recompensa, sem taxas."""
    return reward * BLOCKS_PER_DAY / network_ths(difficulty)


def rental_value(avg_ths: float, days: float, difficulty: float, reward: float, fee_pct: float) -> dict:
    blocks = avg_ths / network_ths(difficulty) * BLOCKS_PER_DAY * days
    return {"blocks_expected": blocks,
            "ev_coin": blocks * reward * (1 - fee_pct / 100),
            "chance_pct": (1 - math.exp(-blocks)) * 100}


class Market:
    """Cotações com cache (uma falha devolve o último valor bom)."""

    def __init__(self, mempool_url: str):
        self.mempool_url = mempool_url
        self._cache: dict[str, tuple[float, dict]] = {}

    async def _cached(self, key: str, fn) -> dict | None:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < MARKET_TTL_S:
            return hit[1]
        try:
            data = await fn()
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ZeroDivisionError) as e:
            log.warning("cotação %s indisponível: %s", key, describe_error(e))
            return hit[1] if hit else None
        self._cache[key] = (time.time(), data)
        return data

    async def btc(self, client: httpx.AsyncClient) -> dict | None:
        async def fetch():
            prices = (await client.get(f"{self.mempool_url}/api/v1/prices", timeout=15)).json()
            blocks = (await client.get(f"{self.mempool_url}/api/v1/blocks", timeout=15)).json()
            rewards = [b["extras"]["reward"] / 1e8 for b in blocks if (b.get("extras") or {}).get("reward")]
            reward = sum(rewards) / len(rewards)
            diff = float(blocks[0]["difficulty"])
            return {"coin": "BTC", "price_eur": float(prices["EUR"]), "difficulty": diff,
                    "reward": reward, "fee_pct": 0.0, "solo_fee_pct": 0.0}
        return await self._cached("btc", fetch)

    async def bch_hashcommander(self, client: httpx.AsyncClient, base_url: str) -> dict | None:
        async def fetch():
            e = (await client.get(f"{base_url}/estado.json", timeout=15)).json()
            return {"coin": "BCH", "price_eur": float(e["preco_bch_eur"]),
                    "difficulty": float(e["dificuldade_rede"]), "reward": float(e["recompensa_bch"]),
                    "fee_pct": float(parse_number(e.get("taxa_pct")) or 0),
                    "solo_fee_pct": float(parse_number(e.get("taxa_solo_pct")) or 0)}
        return await self._cached(f"bch:{base_url}", fetch)


def compute_rental(*, cost_btc: float, hours: float, start: float | None, start_source: str,
                   newer_run_start: float | None, avg_ths: float | None, avg_samples: int,
                   current_ths: float | None, mode: str | None, coin_market: dict | None,
                   btc_market: dict | None, now: float) -> dict:
    """Monta o resumo financeiro de um aluguel (função pura: fácil de testar)."""
    out: dict = {"cost_btc": cost_btc, "hours": hours, "start": start, "start_source": start_source,
                 "mode": mode, "state": "aguardando", "note": None}
    if btc_market:
        out["cost_eur"] = cost_btc * btc_market["price_eur"]
    if start is None:
        out["note"] = "aguardando o aluguel aparecer na pool para saber quando começou"
        return out

    end = start + hours * 3600
    out["end"] = end
    if newer_run_start is not None and newer_run_start > end + 600:
        out["state"] = "novo"
        out["note"] = ("novo aluguel detectado: atualize cost_btc, rental_hours e rental_start "
                       "no config.toml")
        return out
    out["state"] = "ativo" if now < end else "terminado"
    out["progress"] = max(0.0, min(1.0, (now - start) / (end - start)))

    ths = avg_ths if avg_samples else current_ths
    out["avg_ths"] = ths
    out["avg_source"] = "média gravada pelo painel" if avg_samples else "hashrate atual"
    if not ths:
        return out

    days = hours / 24
    out["price_sats_th_day"] = cost_btc * 1e8 / (ths * days)
    if btc_market:
        out["btc_hashprice_sats"] = hashprice(btc_market["difficulty"], btc_market["reward"]) * 1e8
        out["price_ratio"] = out["price_sats_th_day"] / out["btc_hashprice_sats"]

    if coin_market:
        solo = mode == "solo"
        fee = coin_market["solo_fee_pct"] if solo else coin_market["fee_pct"]
        v = rental_value(ths, days, coin_market["difficulty"], coin_market["reward"], fee)
        out.update(coin=coin_market["coin"], ev_coin=v["ev_coin"], blocks_expected=v["blocks_expected"],
                   ev_eur=v["ev_coin"] * coin_market["price_eur"],
                   block_value_eur=coin_market["reward"] * (1 - fee / 100) * coin_market["price_eur"])
        if solo:
            out["chance_pct"] = v["chance_pct"]
        if out.get("cost_eur"):
            out["ev_pct_cost"] = out["ev_eur"] / out["cost_eur"] * 100
    return out
