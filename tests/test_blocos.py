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
                             "extras": {"reward": 312_600_000, "totalFees": 100_000,
                                        "pool": {"name": "Solo CK", "slug": "solock"},
                                        "coinbaseAddress": "bc1qx"}})
    assert b["pool_slug"] == "solock" and b["reward_sats"] == 312_600_000
    assert b["coinbase_addresses"] == ["bc1qx"]
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
