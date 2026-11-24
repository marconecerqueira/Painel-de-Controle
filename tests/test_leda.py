"""Leda: resumo do painel, conversa com o Gemini (falso, sem rede) e o endereço /api/leda."""

import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from minerador.app import create_app
from minerador.config import parse_config
from minerador.leda import Leda, LedaError, pcm_para_wav, resumo

SNAP = {
    "totals": {"hashrate_ths": 141.2, "power_w": 3018, "online": 2, "total": 3,
               "finance": {"profit_eur_month": -12.5}},
    "miners": [
        {"name": "Antminer S19j XP", "coin": "BTC", "status": "online", "reasons": [],
         "local": {"online": True, "hashrate_ths": 140.0, "temp_max_c": 68.4, "power_w": 3000,
                   "pool_url": "stratum+tcp://192.168.1.50:3333", "pool_user": "bc1qsegredo.s19"},
         "pool_worker": {"hashrate_ths": 145.0}, "finance": {"profit_eur_day": -0.42}},
        {"name": "Bitaxe", "coin": "BTC", "status": "offline", "reasons": ["Sem resposta há 12 min"],
         "local": {"online": False}, "pool_worker": {"hashrate_ths": 1.1}},
        {"name": "Escondida", "status": "online", "hidden": True},
    ],
    "alerts": {"active": [{"title": "Bitaxe parada", "detail": "sem resposta", "acked": False},
                          {"title": "Antigo", "detail": "já visto", "acked": True}]},
    "market": {"btc_eur": 56216.4},
}


def test_resumo_tem_o_essencial_e_nada_privado():
    r = resumo(SNAP)
    assert "Antminer S19j XP" in r and "140.00 TH/s" in r and "68 °C" in r and "-0.42 €" in r
    assert "Bitaxe (BTC): parada" in r and "1.10 TH/s" in r and "Sem resposta há 12 min" in r
    assert "Bitaxe parada" in r and "Antigo" not in r and "Escondida" not in r
    assert "-12.50 €" in r and "56216 €" in r
    assert "192.168" not in r and "bc1q" not in r and "stratum" not in r


def test_pcm_para_wav():
    wav = pcm_para_wav(b"\x00\x00" * 24000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) == 44 + 48000


def _google(texto_status=200, tts_status=200):
    """Transporte falso: responde como a API do Gemini (texto e depois voz)."""
    pedidos = []

    def handler(req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content)
        pedidos.append((req.url.path, corpo, req.headers.get("x-goog-api-key")))
        if "tts" in req.url.path:
            if tts_status != 200:
                return httpx.Response(tts_status, json={})
            pcm = base64.b64encode(b"\x01\x00" * 100).decode()
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": pcm}}]}}]})
        if texto_status != 200:
            return httpx.Response(texto_status, json={})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "pensando…", "thought": True}, {"text": "A S19 está ótima, a 68 graus!"}]}}]})
    return httpx.MockTransport(handler), pedidos


def test_responder_texto_e_voz():
    transporte, pedidos = _google()
    leda = Leda("chave-teste", transport=transporte)
    out = asyncio.run(leda.responder("Como está a S19?", SNAP))
    assert out["texto"] == "A S19 está ótima, a 68 graus!"
    assert base64.b64decode(out["audio"])[:4] == b"RIFF"
    (p1, c1, k1), (p2, c2, _) = pedidos
    assert "gemini-flash-lite-latest" in p1 and "Antminer S19j XP" in c1["systemInstruction"]["parts"][0]["text"]
    assert c1["contents"][0]["parts"][0]["text"] == "Como está a S19?" and k1 == "chave-teste"
    assert "tts" in p2 and c2["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Leda"


def test_sem_voz_ainda_responde_por_escrito():
    transporte, _ = _google(tts_status=500)
    out = asyncio.run(Leda("k", transport=transporte).responder("oi", SNAP))
    assert out["texto"] and out["audio"] is None


def test_limite_do_google_vira_mensagem_simples():
    transporte, _ = _google(texto_status=429)
    with pytest.raises(LedaError, match="limite"):
        asyncio.run(Leda("k", transport=transporte).responder("oi", SNAP))


def test_config_e_endereco(tmp_path):
    sem = parse_config({"server": {"db_path": str(tmp_path / "a.db")}})
    assert sem.leda is None
    com = parse_config({"server": {"db_path": str(tmp_path / "b.db")}, "leda": {"api_key": " xyz "}})
    assert com.leda == {"api_key": "xyz", "voice": "Leda", "text_model": "gemini-flash-lite-latest",
                        "tts_model": "gemini-3.8-flash-tts"}
    with TestClient(create_app(sem, start_collector=False)) as c:
        assert c.get("/api/leda").json() == {"ativa": False}
        assert c.post("/api/leda", json={"pergunta": "oi"}).status_code == 403          # sem X-Minerador
        assert c.post("/api/leda", json={"pergunta": "oi"}, headers={"X-Minerador": "1"}).status_code == 503
    with TestClient(create_app(com, start_collector=False)) as c:
        assert c.get("/api/leda").json() == {"ativa": True}
        assert c.post("/api/leda", json={"pergunta": ""}, headers={"X-Minerador": "1"}).status_code == 422


def test_modelo_ocupado_usa_a_reserva():
    vistos = []

    def handler(req):
        vistos.append(req.url.path)
        if "lite" in req.url.path:
            return httpx.Response(503, json={})
        if "tts" in req.url.path:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Oi!"}]}}]})
    out = asyncio.run(Leda("k", transport=httpx.MockTransport(handler)).responder("oi", SNAP))
    assert out["texto"] == "Oi!" and "lite" in vistos[0] and "gemini-flash-latest" in vistos[1]
