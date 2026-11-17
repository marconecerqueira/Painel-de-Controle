"""Hash Commander (pool híbrida BCH): parser, coletor e blocos."""

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import simulador  # noqa: E402

from minerador.blocks import BlockFeed  # noqa: E402
from minerador.collector import Collector  # noqa: E402
from minerador.config import parse_config  # noqa: E402
from minerador.sources import hashcommander as hc  # noqa: E402

ADDR = "qdemo00000000000000000000000000000000000000"


def test_address_id_matches_pool_rule():
    # valor conferido no site da pool para este endereço
    assert hc.address_id(ADDR) == "0628f7236b05053c"
    assert hc.address_id("bitcoincash:" + ADDR.upper()) == "0628f7236b05053c"


def test_parse_pplns_and_solo():
    me = hc.address_id(ADDR)
    miners = {
        "updated": 1000, "pool_hashrate": 267e12,
        "miners": [
            {"id": "outro", "hashrate": 6e12, "online": True, "machines": [{"name": "s9", "hashrate": 6e12}]},
            {"id": me, "share_pct": 0.1, "receives_bch": 0.0019906, "hashrate": 3.58e12, "online": True,
             "best_share": 13319007, "machines": [{"name": "MMlcoin", "hashrate": 3.58e12, "best_share": 2129651}]},
        ],
        "solo_miners": [{"id": me, "hashrate": 223.7e12, "best_share": 13319007,
                         "machines": [{"name": "MMsCoin", "hashrate": 223.7e12, "best_share": 13319007}]}],
    }
    stats = {"pools": {"bitcoincash": {"networkDifficulty": 495226832114.3363}}}
    r = hc.parse(miners, ADDR, pool={"blocksFound": []}, stats=stats, now=1010)
    assert r.ok and r.error is None and r.coin == "BCH"
    w = {x.name: x for x in r.workers}
    assert set(w) == {"MMlcoin", "MMsCoin"}                       # máquinas de outros ficam de fora
    assert w["MMsCoin"].mode == "solo" and w["MMlcoin"].mode == "PPLNS"
    assert w["MMsCoin"].online and w["MMsCoin"].last_seen == 1000
    assert round(r.hashrate_ths, 2) == 227.28
    assert r.pplns_percent == 0.1 and r.payout_estimate == 0.0019906 and r.blocks_found == 0
    assert 100 < 1 / r.solo_blocks_per_day < 125                  # ~1 bloco a cada ~110 dias


def test_rental_ended_is_idle_not_offline():
    me = hc.address_id(ADDR)
    # saiu da janela: continua listado, online=false e sem máquinas
    miners = {"updated": 1, "miners": [{"id": me, "share_pct": 0.1, "hashrate": 0, "online": False,
                                        "machines": []}], "solo_miners": []}
    r = hc.parse(miners, ADDR)
    assert r.ok and r.workers == [] and r.error is None


def test_collector_and_blocks_against_simulator(tmp_path):
    cfg = parse_config({
        "server": {"db_path": str(tmp_path / "t.db")},
        "miners": [{"id": "solo", "name": "Solo", "type": "rental", "pool": "hc", "worker": "aluguel-solo"},
                   {"id": "fim", "name": "Acabou", "type": "rental", "pool": "hc", "worker": "nao-existe"}],
        "pools": {"hc": {"type": "hashcommander", "address": simulador.BCH_ADDR, "base_url": "http://sim/hc"}},
        "blocks": {"base_url": "http://sim/mempool"},
    })

    async def go():
        transport = httpx.ASGITransport(app=simulador.app)
        col = Collector(cfg)
        async with httpx.AsyncClient(transport=transport) as client:
            col._client = client
            await col.poll_pools_once()
        feed = BlockFeed(cfg, transport=transport)
        minhas = await feed.get("minhas")
        await feed.aclose()
        return col.snapshot(), minhas

    snap, minhas = asyncio.run(go())
    m = {x["id"]: x for x in snap["miners"]}
    assert m["solo"]["status"] == "online" and m["solo"]["coin"] == "BCH"
    assert m["solo"]["pool_worker"]["mode"] == "solo"
    assert m["fim"]["status"] == "idle"
    assert snap["pools"][0]["coin"] == "BCH" and snap["pools"][0]["solo_blocks_per_day"] > 0

    bch = [b for b in minhas["blocks"] if b["coin"] == "BCH"]
    assert len(bch) == 1 and bch[0]["found_by_me"] and bch[0]["mine"]
    assert bch[0]["explorer_url"].endswith("/977600") and bch[0]["reward_sats"] == 312_580_000
    assert any("Hash Commander" in n for n in minhas["notes"])
