"""Leda: assistente de voz do painel. Responde perguntas sobre as máquinas e devolve a resposta falada.

Cérebro e voz vêm da API do Gemini (Google AI Studio). A chave fica só no servidor ([leda] api_key no
config.toml); o navegador nunca a vê. Vai para o Google só a pergunta e um resumo curto do painel
(máquinas, temperaturas, alertas, finanças) — nunca endereços, IPs ou nomes de rede.
"""

from __future__ import annotations

import asyncio
import base64
import io
import wave

import httpx

API = "https://generativelanguage.googleapis.com/v1beta/models"
RESERVA = "gemini-flash-latest"          # modelo de texto usado se o principal falhar

STATUS = {"online": "funcionando", "warning": "com atenção", "offline": "parada", "idle": "sem aluguel",
          "sleeping": "dormindo", "unknown": "sem dados"}

PERSONA = (
    "Você é a Leda, a assistente de voz do Painel de Controle do Marcone, que vigia as mineradoras de Bitcoin "
    "dele. Fale em português do Brasil, de forma alegre, simpática e carinhosa, como numa conversa. "
    "Responda em no máximo 3 frases curtas, sem listas, sem markdown e sem emojis, porque a resposta vai ser falada. "
    "Para falar das máquinas use só os dados do painel abaixo; se a informação não estiver lá, diga que não sabe. "
    "Não dê conselhos de investimento. Arredonde os números e diga as unidades por extenso "
    "(terahashes por segundo, graus, watts, euros)."
)
ESTILO_VOZ = "Fale em português do Brasil, com um tom alegre, simpático e carinhoso: "


class LedaError(Exception):
    """Erro com a mensagem já pronta para mostrar ao usuário."""


def resumo(snap: dict) -> str:
    """Texto curto com o estado do painel, para dar contexto à Leda."""
    linhas = []
    t = snap.get("totals") or {}
    if t.get("hashrate_ths") is not None:
        pot = f"{t['power_w']:.0f} W" if t.get("power_w") else "desconhecida"
        linhas.append(f"Hashrate total: {t['hashrate_ths']:.1f} TH/s; máquinas ativas: {t.get('online')} de "
                      f"{t.get('total')}; potência total: {pot}.")
    for m in snap.get("miners") or []:
        if m.get("hidden"):
            continue
        L, P, f = m.get("local") or {}, m.get("pool_worker") or {}, m.get("finance") or {}
        partes = [f"- {m['name']} ({m.get('coin') or 'BTC'}): {STATUS.get(m.get('status'), m.get('status'))}"]
        ths = L.get("hashrate_ths") if L.get("online") else P.get("hashrate_ths")
        if ths is not None:
            partes.append(f"{ths:.2f} TH/s")
        if L.get("temp_max_c") is not None:
            partes.append(f"temperatura {L['temp_max_c']:.0f} °C")
        if L.get("power_w") is not None:
            partes.append(f"{L['power_w']:.0f} W")
        if f.get("profit_eur_day") is not None:
            partes.append(f"resultado {f['profit_eur_day']:+.2f} € por dia")
        if m.get("reasons"):
            partes.append("motivo: " + "; ".join(m["reasons"][:2]))
        linhas.append(", ".join(partes))
    fin = t.get("finance") or {}
    if fin.get("profit_eur_month") is not None:
        linhas.append(f"Resultado do mês até agora (estimativa): {fin['profit_eur_month']:+.2f} €.")
    ativos = [a for a in (snap.get("alerts") or {}).get("active", []) if not a.get("acked")]
    linhas.append("Alertas ativos: " + ("; ".join(f"{a['title']} ({a.get('detail', '')})" for a in ativos[:5])
                                        if ativos else "nenhum."))
    btc = (snap.get("market") or {}).get("btc_eur")
    if btc:
        linhas.append(f"Preço do Bitcoin agora: {btc:.0f} €.")
    return "\n".join(linhas)


def pcm_para_wav(pcm: bytes, taxa: int = 24000) -> bytes:
    """O Gemini devolve PCM 16 bits mono; o navegador precisa de um WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(pcm)
    return buf.getvalue()


class Leda:
    def __init__(self, api_key: str, voice: str = "Leda", text_model: str = "gemini-flash-lite-latest",
                 tts_model: str = "gemini-3.8-flash-tts", transport: httpx.AsyncBaseTransport | None = None):
        self.api_key, self.voice, self.text_model, self.tts_model = api_key, voice, text_model, tts_model
        self.transport = transport                    # testes passam um transporte falso (sem rede)
        self._lock = asyncio.Lock()                   # uma pergunta por vez: o plano grátis tem limite por minuto

    async def _post(self, client: httpx.AsyncClient, model: str, corpo: dict) -> dict:
        r = await client.post(f"{API}/{model}:generateContent", json=corpo, headers={"x-goog-api-key": self.api_key})
        if r.status_code == 429:
            raise LedaError("Estou descansando um minutinho: o limite de uso do Google foi atingido. Tenta de novo já já.")
        if r.status_code in (400, 401, 403):
            raise LedaError("O Google recusou a minha chave. Confira o [leda] api_key no config.toml.")
        r.raise_for_status()
        return r.json()

    async def responder(self, pergunta: str, snap: dict) -> dict:
        """Devolve {"texto": ..., "audio": WAV em base64 ou None (se só a voz falhar)}."""
        pedido = {"systemInstruction": {"parts": [{"text": f"{PERSONA}\n\nDados do painel agora:\n{resumo(snap)}"}]},
                  "contents": [{"role": "user", "parts": [{"text": pergunta}]}],
                  "generationConfig": {"temperature": 0.7}}
        async with self._lock, httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            texto = ""
            # o Google às vezes fica sobrecarregado (503) num modelo: tenta o de reserva antes de desistir
            for model in dict.fromkeys([self.text_model, RESERVA]):
                try:
                    d = await self._post(client, model, pedido)
                    partes = d["candidates"][0]["content"]["parts"]
                    texto = " ".join(p["text"] for p in partes if p.get("text") and not p.get("thought")).strip()
                    break
                except LedaError:
                    raise
                except (httpx.HTTPError, KeyError, IndexError):
                    continue
            else:
                raise LedaError("O Google está ocupado agora. Tenta de novo daqui a pouquinho.")
            if not texto:
                raise LedaError("Fiquei sem palavras desta vez. Pode perguntar de outro jeito?")
            try:
                v = await self._post(client, self.tts_model, {
                    "contents": [{"parts": [{"text": ESTILO_VOZ + texto}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
                })
                pcm = base64.b64decode(v["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                audio = base64.b64encode(pcm_para_wav(pcm)).decode()
            except (LedaError, httpx.HTTPError, KeyError, IndexError, ValueError):
                audio = None                          # a resposta escrita ainda serve
            return {"texto": texto, "audio": audio}
