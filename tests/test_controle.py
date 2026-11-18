"""Controle remoto (Fase 4): LuxOS (sessão + comandos) e AxeOS, contra o simulador."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import simulador  # noqa: E402

from minerador import control  # noqa: E402
from minerador.alerts import AlertEngine, AlertSettings, conditions  # noqa: E402
from minerador.collector import Collector  # noqa: E402
from minerador.config import parse_config  # noqa: E402
from minerador.db import Database  # noqa: E402
from minerador.sources import axeos, luxos  # noqa: E402


@pytest.fixture(autouse=True)
def reset_sim():
    simulador.reset_state()
    yield
    simulador.reset_state()


def lux(coro_fn):
    """Roda coro_fn(port) com o LuxOS simulado escutando numa porta livre."""
    async def go():
        server = await asyncio.start_server(simulador.handle_luxos, "127.0.0.1", 0)
        try:
            return await coro_fn(server.sockets[0].getsockname()[1])
        finally:
            server.close()
    return asyncio.run(go())


def test_parse_reads_profile_and_sleep():
    cfg = {"STATUS": [{"STATUS": "S"}], "CONFIG": [{"Profile": "450MHz", "CurtailMode": "Sleep"}]}
    r = luxos.parse({"STATUS": [{"STATUS": "S"}], "SUMMARY": [{}]}, config=cfg)
    assert r.profile == "450MHz" and r.sleeping is True
    cfg["CONFIG"][0]["CurtailMode"] = "None"
    assert luxos.parse({"SUMMARY": [{}]}, config=cfg).sleeping is False
    a = axeos.parse({"frequency": 525, "coreVoltage": 1150})
    assert a.frequency_mhz == 525 and a.core_voltage_mv == 1150


def test_luxos_options_and_profile_change():
    async def go(port):
        opts = await control.luxos_options("127.0.0.1", port)
        await control.luxos_run("127.0.0.1", port, "profile", "450MHz")
        after = await control.luxos_options("127.0.0.1", port)
        return opts, after
    opts, after = lux(go)
    names = [p["name"] for p in opts["profiles"]]
    assert "default" in names and "450MHz" in names
    assert opts["current_profile"] == "default" and opts["sleeping"] is False
    d = next(p for p in opts["profiles"] if p["name"] == "default")
    assert d["watts"] == 3010 and d["ths"] == 141 and d["step"] == 0
    assert [p["step"] for p in opts["profiles"]] == [-14, -4, -2, 0, 2]     # do mais econômico ao mais forte
    assert after["current_profile"] == "450MHz"
    assert simulador.LUX["session"] == ""                      # sessão fechada depois do comando


def test_luxos_sleep_wakeup_and_reading():
    async def go(port):
        await control.luxos_run("127.0.0.1", port, "sleep")
        asleep = await luxos.fetch("127.0.0.1", port)
        await control.luxos_run("127.0.0.1", port, "wakeup")
        awake = await luxos.fetch("127.0.0.1", port)
        return asleep, awake
    asleep, awake = lux(go)
    assert asleep.sleeping is True and asleep.hashrate_ths == 0
    assert awake.sleeping is False and awake.hashrate_ths > 100


def test_luxos_reboot_and_restart_do_not_leave_session():
    async def go(port):
        await control.luxos_run("127.0.0.1", port, "restart")
        await control.luxos_run("127.0.0.1", port, "reboot")
    lux(go)
    assert simulador.LUX["log"][-2:] == ["resetminer", "rebootdevice"]
    assert simulador.LUX["session"] == ""


def test_luxos_busy_session_and_bad_input():
    simulador.LUX["session"] = "outra123"                       # interface web do LuxOS aberta

    async def busy(port):
        with pytest.raises(control.ControlError, match="sessão"):
            await control.luxos_run("127.0.0.1", port, "sleep")
    lux(busy)
    assert simulador.LUX["session"] == "outra123"              # não derrubou a sessão de ninguém
    simulador.LUX["session"] = ""

    async def bad(port):
        with pytest.raises(control.ControlError, match="perfil"):
            await control.luxos_run("127.0.0.1", port, "profile", "999MHz")
        with pytest.raises(control.ControlError, match="perfil"):
            await control.luxos_run("127.0.0.1", port, "profile", "default,0")
        with pytest.raises(control.ControlError):
            await control.luxos_run("127.0.0.1", port, "formatar")
    lux(bad)
    assert "profileset" not in simulador.LUX["log"]

    async def offline():
        with pytest.raises(control.ControlError, match="sem resposta"):
            await control.luxos_run("127.0.0.1", 1, "sleep")
    asyncio.run(offline())


def axe(fn):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=simulador.app)) as client:
            return await fn(client)
    return asyncio.run(go())


def test_axeos_options_tune_and_restart():
    async def go(client):
        opts = await control.axeos_options(client, "sim", 80)
        with pytest.raises(control.ControlError, match="frequência"):
            await control.axeos_run(client, "sim", 80, "tune", {"frequency": 999, "coreVoltage": 1150})
        with pytest.raises(control.ControlError, match="voltagem"):
            await control.axeos_run(client, "sim", 80, "tune", {"frequency": 525, "coreVoltage": 1400})
        await control.axeos_run(client, "sim", 80, "tune", {"frequency": 575, "coreVoltage": 1200})
        await control.axeos_run(client, "sim", 80, "restart")
        return opts, await control.axeos_options(client, "sim", 80)
    opts, after = axe(go)
    assert 525 in opts["frequency_options"] and 1150 in opts["voltage_options"]
    assert opts["frequency"] == 525 and opts["core_voltage"] == 1150
    assert after["frequency"] == 575 and after["core_voltage"] == 1200
    assert simulador.AXE["log"] == ["patch", "restart"]


def make_collector(tmp_path, lux_port):
    cfg = parse_config({
        "server": {"db_path": str(tmp_path / "t.db")},
        "miners": [{"id": "s19", "name": "S19j XP", "type": "luxos", "host": "127.0.0.1", "port": lux_port,
                    "pool": "defi", "expected_ths": 140},
                   {"id": "bitaxe", "name": "Bitaxe", "type": "axeos", "host": "sim", "pool": "defi"},
                   {"id": "nerd", "name": "Nerd", "type": "pool_only", "pool": "defi", "worker": "nerd"}],
        "pools": {"defi": {"type": "defisurfers", "address": simulador.ADDR, "base_url": "http://sim/defi"}},
    })
    db = Database(cfg.db_path)
    return Collector(cfg, db, AlertEngine(AlertSettings(), db)), db


def test_collector_control_logs_and_sleep_status(tmp_path):
    async def go(port):
        col, db = make_collector(tmp_path, port)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=simulador.app)) as client:
            col._client = client
            opts = await col.control_options("s19")
            with pytest.raises(ValueError):
                await col.control_options("nerd")
            with pytest.raises(KeyError):
                await col.control("nada", "sleep")
            prof = await col.control("s19", "profile", "450MHz", refresh=False)
            tune = await col.control("bitaxe", "tune", {"frequency": 575, "coreVoltage": 1200}, refresh=False)
            assert "450MHz" in prof and "575 MHz, 1200 mV" in tune
            msg = await col.control("s19", "sleep", refresh=False)
            await col.poll_local_once()
            await col.poll_pools_once()
            snap = col.snapshot()
        db.close()
        return opts, msg, snap, col.alerts.recent
    opts, msg, snap, recent = lux(go)
    assert opts["type"] == "luxos" and opts["actions"]
    assert "dormir" in msg.lower()
    s19 = next(m for m in snap["miners"] if m["id"] == "s19")
    assert s19["status"] == "sleeping" and s19["control"] is True
    assert next(m for m in snap["miners"] if m["id"] == "nerd")["control"] is False
    assert recent[0]["event"] == "action" and "S19j XP" in recent[0]["title"]
    conds, _ = conditions(snap, AlertSettings())
    keys = {c.key for c in conds}
    assert "s19:sleeping" in keys and "s19:hashrate" not in keys and "s19:noshares" not in keys


def test_api_control_routes(tmp_path):
    from fastapi.testclient import TestClient
    from minerador.app import create_app

    async def go(port):
        cfg = parse_config({
            "server": {"db_path": str(tmp_path / "t.db")},
            "miners": [{"id": "s19", "name": "S19j XP", "type": "luxos", "host": "127.0.0.1", "port": port},
                       {"id": "nerd", "name": "Nerd", "type": "pool_only", "pool": "defi", "worker": "n"}],
            "pools": {"defi": {"type": "defisurfers", "address": simulador.ADDR, "base_url": "http://sim/defi"}},
        })

        def calls():
            with TestClient(create_app(cfg, start_collector=False)) as c:
                H = {"X-Minerador": "1"}
                return (c.post("/api/miners/s19/control", json={"action": "sleep"}).status_code,
                        c.get("/api/miners/s19/control").json(),
                        c.post("/api/miners/s19/control", json={"action": "sleep"}, headers=H).json(),
                        c.post("/api/miners/s19/control", json={"action": "profile", "value": "x"}, headers=H),
                        c.get("/api/miners/nerd/control").status_code,
                        c.get("/api/miners/nada/control").status_code)
        return await asyncio.to_thread(calls)

    no_header, opts, ok, bad, nerd, missing = lux(go)
    assert no_header == 403
    assert simulador.LUX["log"].count("curtail") == 1          # só o pedido com o cabeçalho executou
    assert opts["current_profile"] == "default"
    assert ok["ok"] is True and "dormir" in ok["message"]
    assert bad.status_code == 409 and "perfil" in bad.json()["detail"]
    assert (nerd, missing) == (400, 404)
