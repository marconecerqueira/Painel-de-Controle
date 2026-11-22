"""Recorde de share (com data) e a procura de mineradores na rede."""

import asyncio

import pytest

from minerador import discover
from minerador.collector import Collector
from minerador.db import Database
from minerador.models import PoolReading, PoolWorker

from test_integracao import _run, make_config


def test_recorde_vem_das_leituras_e_fica_guardado(tmp_path):
    snap, _ = asyncio.run(_run(tmp_path))
    best = {m["id"]: m["best_share"] for m in snap["miners"]}
    assert best["s19"]["value"] == pytest.approx(2.1e11)       # da pool (a máquina diz menos)
    assert best["bitaxe"]["value"] == 1920102528.0             # o maior entre máquina (1,92e9) e pool
    assert best["morto"] is None

    # o painel reinicia: o recorde continua com a data em que apareceu
    cfg = make_config(1, str(tmp_path / "t.db"))
    db = Database(cfg.db_path)
    again = Collector(cfg, db).snapshot()
    db.close()
    assert {m["id"]: m["best_share"] for m in again["miners"]}["s19"] == best["s19"]


def test_recorde_antigo_sai_do_historico_com_a_data_certa(tmp_path):
    cfg = make_config(1, str(tmp_path / "t.db"))
    db = Database(cfg.db_path)
    for ts, bd in ((1000.0, 5e6), (2000.0, 9e6), (3000.0, 9e6), (4000.0, 7e6)):   # o máximo apareceu em 2000
        db._insert_pool(ts, "para", PoolReading(ok=True, workers=[PoolWorker("nerdminer", 1e-7, bd, ts, True)]))
    col = Collector(cfg, db)
    assert col.best["nerd"] == (9e6, 2000.0)
    assert db.best_shares()["nerd"] == (9e6, 2000.0)
    db.close()


def test_procura_reconhece_axeos_e_luxos():
    assert discover.axeos_found("10.0.0.5", {"hostname": "bitaxe", "ASICModel": "BM1370",
                                             "boardVersion": "601", "version": "v2.11"}) == {
        "type": "axeos", "host": "10.0.0.5", "name": "bitaxe", "model": "BM1370 601", "firmware": "v2.11"}
    assert discover.axeos_found("10.0.0.6", {"status": "ok"}) is None          # outro aparelho com HTTP
    assert discover.axeos_found("10.0.0.6", ["lista"]) is None

    lux = {"STATUS": [{"STATUS": "S"}], "VERSION": [{"LUXminer": "2026.10.1", "Type": "Antminer S19j XP"}]}
    assert discover.luxos_found("10.0.0.7", lux)["type"] == "luxos"
    assert discover.luxos_found("10.0.0.8", {"VERSION": [{"CGMiner": "4.9"}]}) is None   # não é LuxOS


def test_procura_so_em_rede_local_pequena():
    assert discover.check_network("192.168.1.7/24").num_addresses == 256
    with pytest.raises(ValueError):
        discover.check_network("8.8.8.0/24")                      # internet, não
    with pytest.raises(ValueError):
        discover.check_network("10.0.0.0/8")                      # grande demais


def test_bloco_do_config_tem_id_novo():
    taken = {"bitaxe"}
    m = {"type": "axeos", "host": "10.0.0.5", "name": "Bitaxe"}
    a, b = discover.config_snippet(m, taken), discover.config_snippet(m, taken)
    assert 'id = "bitaxe-2"' in a and 'id = "bitaxe-3"' in b and 'host = "10.0.0.5"' in a


def test_procura_contra_o_simulador_luxos():
    """LuxOS de verdade em TCP (simulador): a sondagem acha; endereço sem nada responde None."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import simulador
    import httpx

    async def go():
        server = await asyncio.start_server(simulador.handle_luxos, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        orig = discover.luxos.command
        discover.luxos.command = lambda h, p, c, timeout: orig(h, port, c, timeout=timeout)
        try:
            async with httpx.AsyncClient() as client:
                return await discover._probe(client, "127.0.0.1", asyncio.Semaphore(1))
        finally:
            discover.luxos.command = orig
            server.close()

    found = asyncio.run(go())
    assert found and found["type"] == "luxos" and found["model"] == "Antminer S19j XP"
