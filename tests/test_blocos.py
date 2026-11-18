"""Filtro de blocos (mempool.space + defisurfers) contra o simulador."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import simulador  # noqa: E402

from minerador.blocks import BlockFeed  # noqa: E402
from minerador.config import ConfigError, parse_config  # noqa: E402
from minerador.sources import mempool  # noqa: E402


def make_cfg(**blocks):
    return parse_config({
        "pools": {"defi": {"type": "defisurfers", "address": simulador.ADDR, "base_url": "http://sim/defi"}},
        "blocks": {"base_url": "http://sim/mempool", **blocks},
    })


def run(cfg, *views, transport=None):
    async def go():
        feed = BlockFeed(cfg, transport=transport or httpx.ASGITransport(app=simulador.app))
        try:
            return [await feed.get(v) for v in views]
        finally:
            await feed.aclose()
    return asyncio.run(go())


def test_parse_block():
    b = mempool.parse_block({"id": "abc", "height": 10, "timestamp": 5, "tx_count": 7,
                             "size": 1_500_000, "weight": 3_990_000,
                             "extras": {"reward": 312_600_000, "totalFees": 100_000,
                                        "medianFee": 2.5, "feeRange": [1, 2, 3, 250],
                                        "pool": {"name": "Solo CK", "slug": "solock"},
                                        "coinbaseAddress": "bc1qx"}})
    assert b["pool_slug"] == "solock" and b["reward_sats"] == 312_600_000
    assert b["coinbase_addresses"] == ["bc1qx"]
    assert (b["median_fee"], b["fee_min"], b["fee_max"], b["weight"]) == (2.5, 1, 250, 3_990_000)
    assert mempool.parse_block({"height": 1})["pool_slug"] == "unknown"


def test_views():
    rede, minhas, solo, solock = run(make_cfg(my_pools=["parasite"]), "rede", "minhas", "solo", "pool:solock")

    assert len(rede["blocks"]) == 15
    hs = [b["height"] for b in rede["blocks"]]
    assert hs == sorted(hs, reverse=True)

    # Minhas pools = Parasite (mempool) + o bloco que a defisurfers diz ter achado
    pools_ = {b["pool_slug"] for b in minhas["blocks"]}
    assert pools_ == {"parasite", "defisurfers"}
    assert all(b["mine"] for b in minhas["blocks"])
    defi = next(b for b in minhas["blocks"] if b["pool_slug"] == "defisurfers")
    assert defi["height"] == 975850 and defi["found_by_me"] and defi["worker"] == "bitaxe"
    assert defi["paid_me"]                       # coinbase paga o endereço configurado
    assert "1 bloco encontrado" in minhas["notes"][0]

    assert {"solock", "publicpool", "parasite"} <= {b["pool_slug"] for b in solo["blocks"]}
    assert len(solo["blocks"]) <= 25 and solo["error"] is None
    assert {b["pool_slug"] for b in solock["blocks"]} == {"solock"}
    assert not any(b["mine"] for b in solock["blocks"])


def test_invalid_view_and_config():
    with pytest.raises(ValueError):
        run(make_cfg(), "pool:../etc")
    with pytest.raises(ValueError):
        run(make_cfg(), "tudo")
    with pytest.raises(ConfigError):
        make_cfg(my_pools="parasite")


def test_failure_keeps_last_good_data():
    calls = {"fail": False}

    async def handler(request: httpx.Request):
        if calls["fail"]:
            raise httpx.ConnectError("sem rede")
        transport = httpx.ASGITransport(app=simulador.app)
        return await transport.handle_async_request(request)

    async def go():
        feed = BlockFeed(make_cfg(), transport=httpx.MockTransport(handler))
        first = await feed.get("rede")
        calls["fail"] = True
        feed._cache = {k: (0.0, v) for k, (_, v) in feed._cache.items()}   # tudo expirado
        second = await feed.get("rede")
        await feed.aclose()
        return first, second

    first, second = asyncio.run(go())
    assert first["error"] is None
    assert [b["height"] for b in second["blocks"]] == [b["height"] for b in first["blocks"]]
    assert "ConnectError" in second["error"]


def test_ordem_pela_altura_mesmo_com_horario_fora_de_ordem():
    # o minerador grava o horário do bloco; às vezes o mais alto tem horário anterior
    feed = BlockFeed(make_cfg())
    blocks = [{"height": 976308, "ts": 2000, "pool_slug": "f2pool"},
              {"height": 976309, "ts": 1900, "pool_slug": "foundryusa"},
              {"height": 976307, "ts": 1800, "pool_slug": "antpool"}]
    assert [b["height"] for b in feed._mark(blocks, set())] == [976309, 976308, 976307]


def test_btc_e_bch_misturados_seguem_a_data():
    feed = BlockFeed(make_cfg())
    blocks = [{"height": 976309, "ts": 1000, "pool_slug": "parasite"},
              {"height": 925000, "ts": 2000, "pool_slug": "hashcommander", "coin": "BCH"}]
    assert [b["coin"] for b in feed._mark(blocks, set())] == ["BCH", "BTC"]


def test_parse_pending_blocks():
    raw = [{"blockSize": 1_600_000, "blockVSize": 997_000, "nTx": 3100, "totalFees": 4_200_000,
            "medianFee": 3.2, "feeRange": [2.1, 2.5, 3, 5, 40, 302]},
           {"blockSize": 9_000_000, "blockVSize": 5_400_000, "nTx": 21000, "totalFees": 2_000_000,
            "medianFee": 1.0, "feeRange": [1, 1.5]}]
    p = mempool.parse_pending(raw)
    assert [b["eta_min"] for b in p] == [10, 20]
    assert (p[0]["median_fee"], p[0]["fee_min"], p[0]["fee_max"]) == (3.2, 2.1, 302)
    assert p[0]["tx_count"] == 3100 and p[0]["fees_sats"] == 4_200_000 and p[0]["vsize"] == 997_000
    assert p[0]["blocks"] == 1 and p[1]["blocks"] == 6          # o último junta o resto da fila (5,4 MvB)


def test_rede_view_has_pending_blocks_only_there():
    rede, minhas = run(make_cfg(), "rede", "minhas")
    assert len(rede["pending"]) >= 2 and rede["pending"][0]["eta_min"] == 10
    assert rede["pending"][0]["median_fee"] >= rede["pending"][-1]["median_fee"]
    assert minhas["pending"] == []
