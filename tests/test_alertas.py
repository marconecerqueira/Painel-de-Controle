import asyncio
import json

import httpx

from minerador.alerts import AlertEngine, AlertSettings, TelegramNotifier, conditions
from minerador.db import Database

T0 = 1_800_000_000.0


class FakeNotifier:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, text):
        self.sent.append(text)
        return self.ok


def snap(status="online", hashrate=140.0, temp=60.0, pool_ok=True, rental=None, reasons=None):
    miners = [{
        "id": "s19", "name": "S19 <XP>", "type": "luxos", "status": status, "worker": "w",
        "reasons": reasons or (["sem resposta da API"] if status == "offline" else []),
        "limits": {"temp_max_c": 80, "vr_temp_max_c": None, "expected_ths": 140},
        "local": None if status in ("offline", "unknown") else {
            "online": True, "hashrate_ths": hashrate, "hashrate_avg_ths": hashrate,
            "temp_max_c": temp, "temp_vr_c": None, "warnings": [], "expected_ths": None},
        "pool_worker": {"name": "w", "hashrate_ths": hashrate, "online": True},
    }]
    if rental is not None:
        miners.append({"id": "mrr", "name": "Aluguel", "type": "rental", "status": rental, "worker": "MMCoin",
                       "reasons": [], "limits": {}, "local": None,
                       "pool_worker": {"name": "MMCoin", "hashrate_ths": 23.5, "online": True} if rental == "online" else None})
    return {"miners": miners, "pools": [{"id": "defi", "ok": pool_ok, "error": None if pool_ok else "timeout"}]}


def run(engine, s, t):
    return asyncio.run(engine.evaluate(s, now=t))


def test_offline_fires_after_delay_and_resolves():
    n = FakeNotifier()
    e = AlertEngine(AlertSettings(), notifier=n)
    assert run(e, snap("offline"), T0) == []                 # ainda dentro dos 3 min
    assert run(e, snap("offline"), T0 + 120) == []
    msgs = run(e, snap("offline"), T0 + 181)
    assert len(msgs) == 1 and "OFFLINE" in msgs[0]
    assert "S19 &lt;XP&gt;" in n.sent[0]                      # HTML escapado
    assert run(e, snap("offline"), T0 + 400) == []           # não repete
    assert run(e, snap("online"), T0 + 410) == []            # histerese de 60 s
    msgs = run(e, snap("online"), T0 + 471)
    assert len(msgs) == 1 and "voltou a ficar online" in msgs[0] and "durou" in msgs[0]
    assert e.active == {}


def test_flapping_before_delay_is_silent():
    n = FakeNotifier()
    e = AlertEngine(AlertSettings(), notifier=n)
    run(e, snap("offline"), T0)
    run(e, snap("online"), T0 + 60)                          # voltou antes de disparar
    run(e, snap("offline"), T0 + 120)                        # novo episódio: conta do zero
    assert run(e, snap("offline"), T0 + 250) == []
    assert len(run(e, snap("offline"), T0 + 301)) == 1
    assert len(n.sent) == 1


def test_low_hashrate_and_temperature():
    e = AlertEngine(AlertSettings(), notifier=FakeNotifier())
    run(e, snap(hashrate=22.3, temp=85), T0)
    msgs = run(e, snap(hashrate=22.3, temp=85), T0 + 601)
    text = "\n".join(msgs)
    assert "hashrate baixo" in text and "22,3 TH/s" in text and "16%" in text
    assert "temperatura alta" in text and "85 °C" in text


def test_temperatura_so_normaliza_5_graus_abaixo_do_limite():
    e = AlertEngine(AlertSettings(), notifier=FakeNotifier())
    run(e, snap(temp=78), T0)
    assert run(e, snap(temp=78), T0 + 120) == []             # abaixo de 80: nem começa
    run(e, snap(temp=81), T0 + 130)
    msgs = run(e, snap(temp=81), T0 + 200)                   # disparou (limite 80)
    assert "temperatura alta" in msgs[0] and "some abaixo de 75 °C" in msgs[0]
    assert run(e, snap(temp=77), T0 + 300) == []             # 77 °C: continua ativo
    assert run(e, snap(temp=79), T0 + 500) == []             # oscila perto do limite: sem nova mensagem
    run(e, snap(temp=74), T0 + 600)
    msgs = run(e, snap(temp=74), T0 + 661)                   # 74 °C por 1 min: resolvido
    assert len(msgs) == 1 and "temperatura normalizada" in msgs[0]

    # pendente (ainda não disparou) não ganha folga: caiu abaixo do limite, descarta
    run(e, snap(temp=82), T0 + 1000)
    run(e, snap(temp=78), T0 + 1030)
    assert run(e, snap(temp=78), T0 + 1100) == [] and "s19:temp" not in e.active


def test_unknown_status_does_not_resolve():
    e = AlertEngine(AlertSettings(), notifier=FakeNotifier())
    run(e, snap("offline"), T0)
    run(e, snap("offline"), T0 + 200)
    assert run(e, snap("unknown"), T0 + 1000) == []          # sem dados: não declara "voltou"
    assert "s19:offline" in e.active


def test_failed_send_is_retried():
    n = FakeNotifier(ok=False)
    e = AlertEngine(AlertSettings(), notifier=n)
    run(e, snap("offline"), T0)
    run(e, snap("offline"), T0 + 200)
    assert e.active["s19:offline"].notified is None
    n.ok = True
    run(e, snap("offline"), T0 + 215)
    assert len(n.sent) == 2 and e.active["s19:offline"].notified == T0 + 215


def test_state_survives_restart_without_resending(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    n1 = FakeNotifier()
    e1 = AlertEngine(AlertSettings(), db=db, notifier=n1)
    run(e1, snap("offline"), T0)
    run(e1, snap("offline"), T0 + 200)
    assert len(n1.sent) == 1
    n2 = FakeNotifier()
    e2 = AlertEngine(AlertSettings(), db=db, notifier=n2)    # "reiniciou o Pi"
    assert run(e2, snap("offline"), T0 + 300) == []          # não reenvia
    assert len(run(e2, snap("online"), T0 + 400)) == 0       # histerese
    assert "voltou" in run(e2, snap("online"), T0 + 461)[0]  # mas resolve normalmente
    assert e2.summary()["recent"][0]["event"] == "resolved"
    db.close()


def test_rental_start_and_end_are_info():
    e = AlertEngine(AlertSettings(), notifier=FakeNotifier())
    run(e, snap(rental="online"), T0)
    start = run(e, snap(rental="online"), T0 + 181)
    assert len(start) == 1 and start[0].startswith("ℹ️") and "aluguel ativo" in start[0]
    run(e, snap(rental="idle"), T0 + 500)
    end = run(e, snap(rental="idle"), T0 + 681)
    assert len(end) == 1 and end[0].startswith("⏹") and "aluguel terminou" in end[0]


def test_pool_down():
    c, unknown = conditions(snap(pool_ok=False), AlertSettings())
    assert any(x.key == "pool:defi:down" for x in c)
    c, unknown = conditions(snap(pool_ok=None), AlertSettings())
    assert "pool:defi" in unknown


def test_telegram_notifier_payload_and_token_masking(caplog):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(400, json={"ok": False, "description": f"bad {request.url}"})

    n = TelegramNotifier("123:SECRET", "42", transport=httpx.MockTransport(handler))
    ok = asyncio.run(n.send("<b>oi</b>"))
    assert not ok
    assert seen["url"].endswith("/bot123:SECRET/sendMessage")
    assert seen["body"] == {"chat_id": "42", "text": "<b>oi</b>", "parse_mode": "HTML",
                            "disable_web_page_preview": True}
    assert "SECRET" not in caplog.text and "<token>" in caplog.text


def test_alert_fired_before_telegram_is_sent_when_enabled(tmp_path):
    db = Database(str(tmp_path / "b.db"))
    e1 = AlertEngine(AlertSettings(), db=db, notifier=None)      # sem Telegram
    run(e1, snap("offline"), T0)
    run(e1, snap("offline"), T0 + 200)                           # dispara só no painel
    n = FakeNotifier()
    e2 = AlertEngine(AlertSettings(), db=db, notifier=n)         # Telegram configurado + reinício
    msgs = run(e2, snap("offline"), T0 + 300)
    assert len(msgs) == 1 and "OFFLINE" in msgs[0] and "ativo desde" in msgs[0]
    assert run(e2, snap("offline"), T0 + 315) == []              # uma vez só
    assert len(e2.summary()["recent"]) == 1                      # não duplica no histórico
    db.close()
