"""Financeiro dos aluguéis: contas, config e coletor contra o simulador."""

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import simulador  # noqa: E402

from minerador.collector import Collector  # noqa: E402
from minerador.config import ConfigError, parse_config  # noqa: E402
from minerador.db import Database  # noqa: E402
from minerador.finance import compute_rental, hashprice, network_ths, rental_value  # noqa: E402

BCH = {"coin": "BCH", "price_eur": 190.18, "difficulty": 495216621602.6952, "reward": 3.12766642,
       "fee_pct": 1.5, "solo_fee_pct": 2.0}
BTC = {"coin": "BTC", "price_eur": 59332.0, "difficulty": 134823837323272.7, "reward": 3.135,
       "fee_pct": 0.0, "solo_fee_pct": 0.0}


def test_network_math():
    assert 3.5 < network_ths(BCH["difficulty"]) / 1e6 < 3.6               # ~3,54 EH/s
    assert 40 < hashprice(BTC["difficulty"], BTC["reward"]) * 1e8 < 55      # ~47 sats/TH/dia
    v = rental_value(210, 1, BCH["difficulty"], BCH["reward"], 2.0)
    assert 110 < 1 / v["blocks_expected"] < 125                             # 1 bloco a cada ~117 dias
    assert round(v["chance_pct"], 2) == round(v["blocks_expected"] * 100, 2)  # chance pequena ≈ esperado


def test_solo_rental_summary():
    start = 1_000_000.0
    r = compute_rental(cost_btc=0.00118, hours=19, start=start, start_source="config",
                       newer_run_start=start + 1400, avg_ths=230, avg_samples=50, current_ths=200,
                       mode="solo", coin_market=BCH, btc_market=BTC, now=start + 10 * 3600)
    assert r["state"] == "ativo" and round(r["progress"], 3) == round(10 / 19, 3)
    assert round(r["cost_eur"], 2) == 70.01
    assert r["avg_ths"] == 230 and r["avg_source"] == "média gravada pelo painel"
    assert 0.6 < r["chance_pct"] < 0.9
    assert 3.5 < r["ev_eur"] < 5 and 5 < r["ev_pct_cost"] < 7
    assert 600 < r["price_sats_th_day"] < 700 and 12 < r["price_ratio"] < 16
    assert round(r["block_value_eur"]) == round(3.12766642 * 0.98 * 190.18)


def test_pplns_has_no_block_chance_and_ended_state():
    r = compute_rental(cost_btc=0.0000055, hours=9, start=0.0, start_source="config", newer_run_start=None,
                       avg_ths=None, avg_samples=0, current_ths=4.0, mode="PPLNS",
                       coin_market=BCH, btc_market=BTC, now=10 * 3600)
    assert r["state"] == "terminado" and r["progress"] == 1.0
    assert r["avg_source"] == "hashrate atual" and "chance_pct" not in r
    assert r["ev_eur"] > 0


def test_new_rental_after_configured_one_is_flagged():
    r = compute_rental(cost_btc=0.001, hours=2, start=0.0, start_source="config",
                       newer_run_start=5 * 3600, avg_ths=100, avg_samples=3, current_ths=100,
                       mode="solo", coin_market=BCH, btc_market=BTC, now=6 * 3600)
    assert r["state"] == "novo" and "config.toml" in r["note"] and "ev_eur" not in r


def test_waiting_without_start():
    r = compute_rental(cost_btc=0.001, hours=2, start=None, start_source="detectado", newer_run_start=None,
                       avg_ths=None, avg_samples=0, current_ths=None, mode=None,
                       coin_market=BCH, btc_market=BTC, now=0)
    assert r["state"] == "aguardando" and r["note"]


def test_config_validation():
    base = {"pools": {"hc": {"type": "hashcommander", "address": "q", "base_url": "http://x"}}}
    m = {"id": "a", "type": "rental", "pool": "hc", "worker": "w"}
    cfg = parse_config({**base, "miners": [{**m, "cost_btc": 0.001, "rental_hours": 19,
                                            "rental_start": "2026-11-16 23:39"}]})
    assert cfg.miners[0].rental_start == time.mktime(time.strptime("2026-11-16 23:39", "%Y-%m-%d %H:%M"))
    for bad in ({"cost_btc": -1}, {"rental_hours": "19h"}, {"rental_start": "ontem"}):
        with pytest.raises(ConfigError):
            parse_config({**base, "miners": [{**m, **bad}]})


def test_collector_finance_against_simulator(tmp_path):
    now = time.time()
    cfg = parse_config({
        "server": {"db_path": str(tmp_path / "t.db")},
        "miners": [{"id": "solo", "name": "Solo", "type": "rental", "pool": "hc", "worker": "aluguel-solo",
                    "cost_btc": 0.00118, "rental_hours": 19},
                   {"id": "pp", "name": "PPLNS", "type": "rental", "pool": "hc", "worker": "aluguel-pplns",
                    "cost_btc": 0.0000055, "rental_hours": 9}],
        "pools": {"hc": {"type": "hashcommander", "address": simulador.BCH_ADDR, "base_url": "http://sim/hc"}},
        "blocks": {"base_url": "http://sim/mempool"},
    })

    async def go():
        db = Database(cfg.db_path)
        col = Collector(cfg, db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=simulador.app)) as client:
            col._client = client
            await col.poll_pools_once()
            await col.finance_once()
        snap = col.snapshot()
        db.close()
        return snap

    snap = asyncio.run(go())
    m = {x["id"]: x for x in snap["miners"]}
    solo, pp = m["solo"]["rental"], m["pp"]["rental"]
    assert solo["state"] == "ativo" and solo["start_source"] == "detectado"
    assert abs(solo["start"] - now) < 60 and solo["mode"] == "solo" and solo["coin"] == "BCH"
    assert solo["chance_pct"] > 0 and round(solo["cost_eur"], 2) == 70.01
    assert pp["mode"] == "PPLNS" and "chance_pct" not in pp
