"""Roda o coletor de verdade contra o simulador (TCP real para LuxOS, ASGI para o HTTP)."""

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import simulador  # noqa: E402

from minerador.collector import Collector  # noqa: E402
from minerador.config import parse_config  # noqa: E402
from minerador.db import Database  # noqa: E402


def make_config(lux_port: int, db_path: str):
    return parse_config({
        "server": {"db_path": db_path},
        "miners": [
            {"id": "s19", "name": "S19j XP", "type": "luxos", "host": "127.0.0.1", "port": lux_port,
             "pool": "defi", "expected_ths": 140, "temp_max_c": 80},
            {"id": "bitaxe", "name": "Bitaxe", "type": "axeos", "host": "sim", "pool": "defi"},
            {"id": "nerd", "name": "NerdMiner", "type": "pool_only", "pool": "para", "worker": "nerdminer"},
            {"id": "morto", "name": "Desligado", "type": "luxos", "host": "127.0.0.1", "port": 1},
        ],
        "pools": {
            "defi": {"type": "defisurfers", "address": simulador.ADDR, "base_url": "http://sim/defi"},
            "para": {"type": "parasite", "address": simulador.ADDR, "base_url": "http://sim/parasite"},
        },
    })


async def _run(tmp_path):
    server = await asyncio.start_server(simulador.handle_luxos, "127.0.0.1", 0)
    lux_port = server.sockets[0].getsockname()[1]
    cfg = make_config(lux_port, str(tmp_path / "t.db"))
    db = Database(cfg.db_path)
    col = Collector(cfg, db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=simulador.app)) as client:
        col._client = client
        await col.poll_local_once()
        await col.poll_pools_once()
    server.close()
    snap = col.snapshot()
    rows = db._conn.execute("SELECT COUNT(*) FROM miner_readings").fetchone()[0]
    db.close()
    return snap, rows


def test_collector_against_simulator(tmp_path):
    snap, rows = asyncio.run(_run(tmp_path))
    m = {x["id"]: x for x in snap["miners"]}

    assert m["s19"]["status"] == "online", m["s19"]["reasons"]
    assert m["s19"]["worker"] == "s19"                 # deduzido do usuário stratum
    assert m["s19"]["pool_worker"]["online"]
    assert 130 < m["s19"]["local"]["hashrate_ths"] < 150
    assert m["s19"]["local"]["power_w"] > 2900

    assert m["bitaxe"]["status"] == "online", m["bitaxe"]["reasons"]
    assert m["bitaxe"]["pool_worker"]["name"] == "bitaxe"

    assert m["nerd"]["status"] == "online"             # só via pool
    assert m["morto"]["status"] == "offline"

    assert snap["totals"]["online"] == 3 and snap["totals"]["total"] == 4
    pools = {p["id"]: p for p in snap["pools"]}
    assert pools["defi"]["ok"] and pools["defi"]["pplns_percent"] == 13.9
    assert pools["para"]["ok"]
    assert rows == 3                                    # s19, bitaxe, morto gravados


def test_transient_pool_failure_keeps_last_good(tmp_path):
    """Uma falha passageira da pool não deve derrubar o NerdMiner (evita alarmes falsos)."""
    from minerador.models import PoolReading, PoolWorker
    cfg = make_config(1, str(tmp_path / "t.db"))
    col = Collector(cfg)
    import time as _t
    now = _t.time()
    good = PoolReading(ok=True, workers=[PoolWorker("nerdminer", 5e-8, 1e6, now - 30, True)])
    col.pools["para"] = (now - 60, good)
    col.pools_last_ok["para"] = (now - 60, good)
    col.pools["para"] = (now, PoolReading(ok=False, error="ReadTimeout"))
    m = {x["id"]: x for x in col.snapshot()["miners"]}
    assert m["nerd"]["status"] == "online"
    p = {x["id"]: x for x in col.snapshot()["pools"]}["para"]
    assert p["ok"] and "ReadTimeout" in p["error"] and p["workers"]

    # depois da janela de tolerância, aí sim vira "indisponível"
    col.pools_last_ok["para"] = (now - col.pool_grace_s - 1, good)
    m = {x["id"]: x for x in col.snapshot()["miners"]}
    assert m["nerd"]["status"] == "unknown"


def test_rental_idle_is_not_offline(tmp_path):
    """Aluguel sem contrato ativo = 'idle' (sem alarme) e não conta no total de máquinas."""
    import time as _t
    from minerador.models import PoolReading, PoolWorker
    cfg = parse_config({
        "miners": [
            {"id": "s19", "name": "S19", "type": "pool_only", "pool": "defi", "worker": "MMcoin8", "expected_ths": 140},
            {"id": "mrr", "name": "Aluguel", "type": "rental", "pool": "defi", "worker": "MMCoin", "expected_ths": 25},
        ],
        "pools": {"defi": {"type": "defisurfers", "address": "1abc", "base_url": "http://x"}},
    })
    col = Collector(cfg)
    now = _t.time()

    def feed(workers):
        r = PoolReading(ok=True, workers=workers)
        col.pools["defi"] = col.pools_last_ok["defi"] = (now, r)
        return {x["id"]: x for x in col.snapshot()["miners"]}, col.snapshot()["totals"]

    # sem aluguel: idle, não entra no total; S19 com 29 TH/s de 140 esperados -> warning
    m, t = feed([PoolWorker("MMcoin8", 29.3, None, now, True)])
    assert m["mrr"]["status"] == "idle"
    assert m["s19"]["status"] == "warning" and "abaixo" in m["s19"]["reasons"][0]
    assert t["total"] == 1 and t["online"] == 1

    # aluguel ativo entregando o contratado; nomes diferem só na caixa (MMCoin != MMcoin)
    m, t = feed([PoolWorker("MMcoin8", 29.3, None, now, True), PoolWorker("MMCoin", 23.5, None, now, True),
                 PoolWorker("MMcoin", 0.9, None, now, True)])
    assert m["mrr"]["status"] == "online" and m["mrr"]["pool_worker"]["hashrate_ths"] == 23.5
    assert t["total"] == 2 and round(t["hashrate_ths"], 1) == 52.8


def test_history_endpoint_data(tmp_path):
    """Histórico: linhas por máquina vêm do banco; total vem da pool (simulador)."""
    async def run():
        server = await asyncio.start_server(simulador.handle_luxos, "127.0.0.1", 0)
        cfg = make_config(server.sockets[0].getsockname()[1], str(tmp_path / "h.db"))
        db = Database(cfg.db_path)
        col = Collector(cfg, db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=simulador.app)) as client:
            col._client = client
            for _ in range(2):
                await col.poll_local_once()
                await col.poll_pools_once()
            h = await col.history("24h")
        server.close()
        db.close()
        return h

    h = asyncio.run(run())
    s = {x["id"]: x for x in h["series"]}
    assert h["bucket_s"] == 600
    assert s["s19"]["source"] == "máquina" and 130 < s["s19"]["points"][-1][1] < 150
    assert s["nerd"]["source"] == "pool" and s["nerd"]["points"]
    assert s["morto"]["points"] == []                     # offline não gera ponto
    total = h["pool_totals"][0]
    assert total["id"] == "defi" and len(total["points"]) >= 100


def test_history_rejects_bad_range():
    import pytest
    col = Collector(make_config(1, ":memory:"))
    with pytest.raises(ValueError):
        asyncio.run(col.history("1ano"))
