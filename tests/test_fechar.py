"""Fechar cards de aluguéis encerrados e fechar/apagar alertas."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import simulador  # noqa: E402

from minerador.alerts import AlertEngine, AlertSettings  # noqa: E402
from minerador.app import create_app  # noqa: E402
from minerador.collector import Collector  # noqa: E402
from minerador.config import parse_config  # noqa: E402
from minerador.db import Database  # noqa: E402

H = {"X-Minerador": "1"}


def cfg_for(tmp_path):
    return parse_config({
        "server": {"db_path": str(tmp_path / "t.db")},
        "miners": [{"id": "ativo", "name": "Ativo", "type": "rental", "pool": "hc", "worker": "aluguel-solo"},
                   {"id": "fim", "name": "Acabou", "type": "rental", "pool": "hc", "worker": "nao-existe"},
                   {"id": "s19", "name": "S19", "type": "pool_only", "pool": "hc", "worker": "nao-existe"}],
        "pools": {"hc": {"type": "hashcommander", "address": simulador.BCH_ADDR, "base_url": "http://sim/hc"}},
    })


def test_hide_ended_rental_and_reopen_when_active(tmp_path):
    cfg = cfg_for(tmp_path)

    async def go():
        db = Database(cfg.db_path)
        col = Collector(cfg, db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=simulador.app)) as client:
            col._client = client
            await col.poll_pools_once()
            await col.set_hidden("fim", True)
            with pytest.raises(ValueError):
                await col.set_hidden("ativo", True)        # em vigor: não fecha
            with pytest.raises(ValueError):
                await col.set_hidden("s19", True)          # máquina própria: não fecha
            with pytest.raises(KeyError):
                await col.set_hidden("nada", True)
            snap = {m["id"]: m for m in col.snapshot()["miners"]}
            assert snap["fim"]["hidden"] and not snap["ativo"]["hidden"]

            # sobrevive a reinício
            assert Collector(cfg, db).hidden == {"fim"}

            # o mesmo aluguel volta a funcionar -> card reaparece sozinho
            cfg.miners[1].worker = "aluguel-pplns"
            await col.poll_pools_once()
            assert "fim" not in col.hidden and db.hidden_miners() == set()
        db.close()

    asyncio.run(go())


def test_api_requires_header_and_handles_alerts(tmp_path):
    cfg = cfg_for(tmp_path)
    app = create_app(cfg, start_collector=False)
    col = app.state.collector
    with TestClient(app) as c:
        assert c.post("/api/miners/fim/hide").status_code == 403          # sem cabeçalho
        assert c.post("/api/miners/fim/hide", headers=H).status_code == 200
        assert c.post("/api/miners/xx/hide", headers=H).status_code == 404
        assert c.get("/api/status").json()["miners"][1]["hidden"] is True
        assert c.post("/api/miners/fim/show", headers=H).status_code == 200
        assert c.get("/api/status").json()["miners"][1]["hidden"] is False

        # um alerta ativo e notificado + dois eventos no histórico
        eng: AlertEngine = col.alerts
        snap = {"miners": [{"id": "x", "name": "X", "type": "luxos", "status": "offline",
                            "reasons": ["sem resposta"], "local": None, "pool_worker": None, "limits": {}}],
                "pools": []}
        asyncio.run(eng.evaluate(snap, now=0))
        asyncio.run(eng.evaluate(snap, now=10_000))
        active = c.get("/api/status").json()["alerts"]["active"]
        assert len(active) == 1 and active[0]["acked"] is False
        key = active[0]["key"]

        assert c.post("/api/alerts/ack", json={"key": key}).status_code == 403
        assert c.post("/api/alerts/ack", json={"key": key}, headers=H).json()["changed"] == 1
        assert c.get("/api/status").json()["alerts"]["active"][0]["acked"] is True
        assert c.post("/api/alerts/ack", json={"undo": True}, headers=H).json()["changed"] == 1

        recent = c.get("/api/status").json()["alerts"]["recent"]
        assert len(recent) == 1
        e = recent[0]
        r = c.post("/api/alerts/events/delete", json={"ts": e["ts"], "key": e["key"], "event": e["event"]}, headers=H)
        assert r.json()["deleted"] is True
        assert c.get("/api/status").json()["alerts"]["recent"] == []
        assert c.post("/api/alerts/events/clear", headers=H).status_code == 200
    # apagado também no banco: um motor novo não traz o evento de volta
    db = Database(cfg.db_path)
    assert AlertEngine(AlertSettings(), db).recent == []
    assert AlertEngine(AlertSettings(), db).active[key].acked is False
    db.close()
