"""Recorde de share (com data)."""

import asyncio

import pytest

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
