"""Erros achados na revisão de 26/11/2026 (vistos no log do painel real)."""

import asyncio
import time

import httpx
import pytest

from minerador.alerts import AlertEngine, AlertSettings, AlertState
from minerador.collector import Collector
from minerador.config import ConfigError, parse_config
from minerador.finance import Market

T0 = 1_800_000_000.0


def run(coro):
    return asyncio.run(coro)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return True


# ---------------------------------------------------------------- × do aviso (erro 500 no log)
def _state(key):
    return AlertState(key, "m", "alert", key, "d", "ok", first_seen=T0, notified=T0)


def test_ack_survives_alert_resolved_while_saving():
    """Fechar o aviso com ×: o laço de alertas pode apagar outro alerta no meio (dictionary changed size)."""
    e = AlertEngine(AlertSettings(), notifier=FakeNotifier())
    e.active = {"a": _state("a"), "b": _state("b")}

    async def db_que_cede(method, *args):
        e.active.pop("b", None)          # o laço de alertas resolveu o "b" enquanto o "a" era gravado
        await asyncio.sleep(0)

    e._db = db_que_cede
    assert run(e.ack(None)) >= 1


# ---------------------------------------------------------------- cotação do BTC (AttributeError no log)
def _market_client(blocks_body, status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/prices"):
            return httpx.Response(200, json={"EUR": 90000})
        return httpx.Response(status, json=blocks_body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("body,status", [(["erro"], 200), ({"error": "x"}, 200), ([], 200), ({"x": 1}, 502)])
def test_market_btc_survives_unexpected_api_answer(body, status):
    async def go():
        async with _market_client(body, status) as c:
            return await Market("https://mempool.test").btc(c)
    assert run(go()) is None             # sem cotação, mas sem explodir


# ---------------------------------------------------------------- laço de coleta
def _cfg(tmp_path, miners, **extra):
    return parse_config({"server": {"db_path": str(tmp_path / "t.db")}, "miners": miners, **extra})


def test_loop_survives_an_unexpected_exception(tmp_path):
    col = Collector(_cfg(tmp_path, []))
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("falha qualquer")

    async def go():
        task = asyncio.create_task(col._loop(fn, 0))
        await asyncio.sleep(1.5)         # o laço espera no mínimo 1 s entre as rodadas
        task.cancel()

    run(go())
    assert len(calls) >= 2               # sem o conserto, o laço morre na 1ª falha


def test_machine_with_garbage_answer_is_not_left_online(tmp_path):
    """Máquina que respondeu bem e depois mandou lixo não pode ficar 'online' com a leitura velha."""
    good = {"hashRate": 1200.0, "expectedHashrate": 1200, "temp": 50, "power": 15}
    answers = [good, None]               # 2ª resposta: JSON "null", que o parser não entende

    def handler(req):
        if answers[0] is None:
            return httpx.Response(200, content=b"null")   # JSON válido, mas sem o formato esperado
        return httpx.Response(200, json=answers[0])

    cfg = _cfg(tmp_path, [{"id": "bx", "name": "Bitaxe", "type": "axeos", "host": "sim"}])
    col = Collector(cfg)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            col._client = c
            await col.poll_local_once()
            first = col.snapshot()["miners"][0]["status"]
            answers[0] = None
            await col.poll_local_once()
            return first, col.snapshot()["miners"][0]["status"]

    first, second = run(go())
    assert first == "online" and second == "offline"


# ---------------------------------------------------------------- alarme falso depois que o Mac dorme
def test_alert_wait_restarts_after_the_mac_sleeps(tmp_path, monkeypatch):
    """O Mac dormiu 1 h: o relógio correu, mas ninguém olhou. A espera do alerta recomeça ao acordar."""
    cfg = _cfg(tmp_path, [{"id": "morto", "name": "Desligado", "type": "luxos", "host": "127.0.0.1", "port": 1}])
    notifier = FakeNotifier()
    col = Collector(cfg, alerts=AlertEngine(cfg.alerts, notifier=notifier))
    clock = [T0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    async def go():
        async with httpx.AsyncClient() as c:
            col._client = c
            await col.poll_local_once()
        col._pools_ready = True
        await col.evaluate_alerts_once()             # a máquina aparece offline: começa a espera de 3 min
        clock[0] += 15
        await col.evaluate_alerts_once()
        clock[0] += 3600                             # o Mac dormiu
        await col.evaluate_alerts_once()
        sent_on_wake = len(notifier.sent)
        for _ in range(2):                           # acordado e a máquina continua offline de verdade
            clock[0] += 100
            await col.evaluate_alerts_once()
        return sent_on_wake, len(notifier.sent)

    on_wake, later = run(go())
    assert on_wake == 0                              # sem o conserto, dispara na hora ao acordar
    assert later == 1                                # e um problema que continua ainda dispara


# ---------------------------------------------------------------- config.toml com texto no lugar de número
@pytest.mark.parametrize("key", ["expected_ths", "temp_max_c", "vr_temp_max_c"])
def test_config_rejects_text_instead_of_number(key):
    with pytest.raises(ConfigError, match=key):
        parse_config({"miners": [{"id": "x", "type": "axeos", "host": "h", key: "34"}]})
