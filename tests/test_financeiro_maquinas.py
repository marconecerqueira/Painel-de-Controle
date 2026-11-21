"""Financeiro das máquinas próprias: receita esperada × custo de energia."""

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
from minerador.finance import compute_own, hashprice  # noqa: E402
from minerador.models import PoolReading, PoolWorker  # noqa: E402

BTC = {"coin": "BTC", "price_eur": 59332.0, "difficulty": 134823837323272.7, "reward": 3.135,
       "fee_pct": 0.0, "solo_fee_pct": 0.0}


def own(**kw):
    base = dict(avg_ths=140.0, avg_samples=100, current_ths=139.0, power_w=3010.0, power_source="config",
                tariff_eur_kwh=0.14, fixed_eur_month=None, market=BTC, fee_pct=0.0, expected_ths=None)
    return compute_own(**{**base, **kw})


def test_s19_at_014_eur_kwh_loses_money():
    r = own()
    hp_eur = hashprice(BTC["difficulty"], BTC["reward"]) * BTC["price_eur"]
    assert r["avg_ths"] == 140 and r["avg_source"] == "média das últimas 24 h"
    assert round(r["kwh_day"], 2) == 72.24
    assert round(r["energy_eur_day"], 2) == round(72.24 * 0.14, 2)          # 10,11 €
    assert r["revenue_eur_day"] == pytest.approx(140 * hp_eur)
    assert 3 < r["revenue_eur_day"] < 5
    assert r["profit_eur_day"] == pytest.approx(r["revenue_eur_day"] - r["energy_eur_day"])
    assert r["profit_eur_day"] < 0
    assert r["profit_eur_month"] == pytest.approx(r["profit_eur_day"] * 30)
    assert r["breakeven_eur_kwh"] == pytest.approx(r["revenue_eur_day"] / r["kwh_day"])
    assert 0.04 < r["breakeven_eur_kwh"] < 0.07


def test_pool_fee_reduces_revenue():
    assert own(fee_pct=2.0)["revenue_eur_day"] == pytest.approx(own()["revenue_eur_day"] * 0.98)


def test_expected_hashrate_shows_what_it_would_earn():
    r = own(avg_ths=25.0, expected_ths=140)
    assert r["expected_revenue_eur_day"] == pytest.approx(own()["revenue_eur_day"])
    assert r["expected_profit_eur_day"] == pytest.approx(own()["profit_eur_day"])
    assert "expected_revenue_eur_day" not in own()


def test_fixed_monthly_fee_instead_of_tariff():
    r = own(fixed_eur_month=90.0, tariff_eur_kwh=None)
    assert r["energy_basis"] == "fixo" and r["energy_eur_day"] == pytest.approx(3.0)
    assert "breakeven_eur_kwh" not in r


def test_without_power_or_tariff_there_is_a_note():
    r = own(power_w=None)
    assert r["energy_eur_day"] is None and "power_w" in r["note"]
    assert r["revenue_eur_day"] > 0 and r["profit_eur_day"] is None
    r = own(tariff_eur_kwh=None)
    assert r["energy_eur_day"] is None and "energy_eur_kwh" in r["note"]


def test_falls_back_to_current_hashrate_and_handles_missing_market():
    r = own(avg_ths=None, avg_samples=0, current_ths=1.1, power_w=15.0)
    assert r["avg_ths"] == 1.1 and r["avg_source"] == "hashrate atual"
    r = own(market=None)
    assert r["revenue_eur_day"] is None and r["energy_eur_day"] > 0 and r["profit_eur_day"] is None


def test_config_finance_fields():
    base = {"pools": {"d": {"type": "defisurfers", "address": "x", "base_url": "http://x", "fee_pct": 1.5}}}
    m = {"id": "s19", "type": "pool_only", "pool": "d", "worker": "w"}
    cfg = parse_config({**base, "finance": {"energy_eur_kwh": 0.14},
                        "miners": [{**m, "power_w": 3010, "energy_eur_month": 50}]})
    assert cfg.energy_eur_kwh == 0.14 and cfg.pools["d"].fee_pct == 1.5
    assert cfg.miners[0].power_w == 3010 and cfg.miners[0].energy_eur_month == 50
    assert parse_config(base).energy_eur_kwh is None
    for bad in ({"finance": {"energy_eur_kwh": "0,14"}}, {"finance": {"energy_eur_kwh": -1}},
                {"miners": [{**m, "power_w": 0}]}):
        with pytest.raises(ConfigError):
            parse_config({**base, **bad})
    with pytest.raises(ConfigError):
        parse_config({"pools": {"d": {**base["pools"]["d"], "fee_pct": 150}}})


def test_collector_own_finance_against_simulator(tmp_path):
    cfg = parse_config({
        "server": {"db_path": str(tmp_path / "t.db")},
        "finance": {"energy_eur_kwh": 0.14},
        "miners": [{"id": "s19", "name": "S19", "type": "pool_only", "pool": "defi", "worker": "s19",
                    "power_w": 3010, "expected_ths": 140},
                   {"id": "bitaxe", "name": "Bitaxe", "type": "axeos", "host": "sim", "pool": "defi",
                    "power_w": 99},
                   {"id": "nerd", "name": "Nerd", "type": "pool_only", "pool": "para", "worker": "nerdminer"}],
        "pools": {"defi": {"type": "defisurfers", "address": simulador.ADDR, "base_url": "http://sim/defi"},
                  "para": {"type": "parasite", "address": simulador.ADDR, "base_url": "http://sim/parasite"}},
        "blocks": {"base_url": "http://sim/mempool"},
    })

    async def go():
        db = Database(cfg.db_path)
        col = Collector(cfg, db)
        # meio dia antes, o S19 estava parado (worker offline): a média de 24 h conta isso como 0
        await db.add_pool_reading(time.time() - 3600, "defi", PoolReading(
            ok=True, workers=[PoolWorker("s19", 0.0, None, None, online=False)]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=simulador.app)) as client:
            col._client = client
            await col.poll_local_once()
            await col.poll_pools_once()
            await col.finance_once()
        snap = col.snapshot()
        db.close()
        return snap

    snap = asyncio.run(go())
    m = {x["id"]: x for x in snap["miners"]}
    s19, bitaxe, nerd = m["s19"]["finance"], m["bitaxe"]["finance"], m["nerd"]["finance"]
    assert 60 < s19["avg_ths"] < 80 and s19["avg_source"] == "média das últimas 24 h"
    assert s19["power_source"] == "nominal (config)" and s19["energy_eur_day"] > 10
    assert s19["expected_revenue_eur_day"] > s19["revenue_eur_day"] > 0
    assert bitaxe["power_source"] == "medido" and bitaxe["power_w"] < 99   # leitura vence o config
    assert nerd["energy_eur_day"] is None and "power_w" in nerd["note"]
    t = snap["totals"]["finance"]
    assert t["energy_eur_day"] == pytest.approx(s19["energy_eur_day"] + bitaxe["energy_eur_day"])
    assert t["revenue_eur_day"] == pytest.approx(
        s19["revenue_eur_day"] + bitaxe["revenue_eur_day"] + nerd["revenue_eur_day"])
    assert t["missing_power"] == ["Nerd"]
    assert snap["market"]["btc_eur"] > 0


def test_tariff_goes_with_the_result_for_the_why_sentence():
    assert own()["tariff_eur_kwh"] == 0.14                      # a tela compara com o ponto de empate
    assert "tariff_eur_kwh" not in own(fixed_eur_month=30.0)     # taxa fixa: não há tarifa por kWh
