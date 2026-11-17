"""Motor de alertas: transforma o snapshot em condições e notifica (Telegram) com atraso e histerese.

Ciclo de vida de uma condição:
  aparece -> espera `delay_s` ainda presente -> DISPARA (notifica)
  some    -> espera `clear_s` ausente      -> RESOLVE (notifica "voltou ao normal")
Se sumir antes de disparar, é descartada em silêncio (oscilação).
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("minerador.alerts")


@dataclass
class AlertSettings:
    offline_s: float = 180
    hashrate_s: float = 600
    temp_s: float = 60
    pool_s: float = 600
    rental_s: float = 180
    clear_s: float = 60            # tempo "normal" exigido antes de resolver
    hashrate_clear_s: float = 300  # hashrate oscila mais: exige 5 min normal
    low_hashrate_ratio: float = 0.7


@dataclass
class Condition:
    key: str                  # único, ex.: "s19jxp:offline"
    scope: str                # id da máquina ou "pool:<id>" (para status desconhecido)
    title: str                # "Antminer S19j XP: offline"
    detail: str
    delay_s: float
    resolved_title: str
    kind: str = "alert"       # alert | info
    clear_s: float = 60


@dataclass
class AlertState:
    key: str
    scope: str
    kind: str
    title: str
    detail: str
    resolved_title: str
    first_seen: float
    notified: float | None = None
    sent_telegram: bool = False   # já foi para o Telegram? (pode ter disparado antes de configurar)
    cleared_since: float | None = None
    clear_s: float = 60
    acked: bool = False           # fechado no aviso do painel (continua ativo até resolver)


# ------------------------------------------------------------------ regras
def _fmt(v: float) -> str:
    return f"{v:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _hr(ths: float) -> str:
    if ths >= 1:
        return f"{_fmt(ths)} TH/s"
    if ths >= 1e-3:
        return f"{_fmt(ths * 1e3)} GH/s"
    return f"{_fmt(ths * 1e6)} MH/s"


def conditions(snap: dict, cfg: AlertSettings) -> tuple[list[Condition], set[str]]:
    """Retorna (condições ativas agora, escopos com estado desconhecido)."""
    out: list[Condition] = []
    unknown: set[str] = set()

    for m in snap["miners"]:
        mid, name, status = m["id"], m["name"], m["status"]
        L, P, lim = m.get("local"), m.get("pool_worker"), m.get("limits") or {}
        if status == "unknown":
            unknown.add(mid)
            continue

        if m["type"] == "rental":
            if status in ("online", "warning") and P:
                out.append(Condition(
                    f"{mid}:rental", mid, f"{name}: aluguel ativo",
                    f"A pool está recebendo {_hr(P['hashrate_ths'])} do worker {m.get('worker')}.",
                    cfg.rental_s, f"{name}: aluguel terminou", kind="info", clear_s=cfg.rental_s))
        elif status == "offline":
            reason = (m.get("reasons") or ["sem resposta"])[0]
            out.append(Condition(f"{mid}:offline", mid, f"{name}: OFFLINE", reason,
                                 cfg.offline_s, f"{name}: voltou a ficar online", clear_s=cfg.clear_s))
            continue

        local_on = bool(L and L.get("online"))
        # hashrate baixo
        expected = lim.get("expected_ths") or (L.get("expected_ths") if L else None)
        if local_on:
            current, where = (L.get("hashrate_avg_ths") or L.get("hashrate_ths")), "máquina reporta"
        else:
            current, where = (P["hashrate_ths"] if P else None), "pool vê"
        if expected and current is not None and current < expected * cfg.low_hashrate_ratio:
            out.append(Condition(
                f"{mid}:hashrate", mid, f"{name}: hashrate baixo",
                f"{where} {_hr(current)}; esperado {_hr(expected)} ({current / expected:.0%}).",
                cfg.hashrate_s, f"{name}: hashrate normalizado", clear_s=cfg.hashrate_clear_s))

        if local_on:
            t, tmax = L.get("temp_max_c"), lim.get("temp_max_c")
            if t is not None and tmax and t >= tmax:
                out.append(Condition(f"{mid}:temp", mid, f"{name}: temperatura alta",
                                     f"{t:.0f} °C (limite {tmax:.0f} °C).", cfg.temp_s,
                                     f"{name}: temperatura normalizada", clear_s=cfg.clear_s))
            vr, vrmax = L.get("temp_vr_c"), lim.get("vr_temp_max_c")
            if vr is not None and vrmax and vr >= vrmax:
                out.append(Condition(f"{mid}:vrtemp", mid, f"{name}: regulador (VR) quente",
                                     f"{vr:.0f} °C (limite {vrmax:.0f} °C).", cfg.temp_s,
                                     f"{name}: VR normalizado", clear_s=cfg.clear_s))
            for w in L.get("warnings") or []:
                if "SUPERAQUECIMENTO" in w:
                    out.append(Condition(f"{mid}:overheat", mid, f"{name}: SUPERAQUECIMENTO", w,
                                         0, f"{name}: saiu do modo de superaquecimento",
                                         clear_s=cfg.clear_s))
                elif "ventoinha parada" in w:
                    out.append(Condition(f"{mid}:fan", mid, f"{name}: ventoinha parada", w,
                                         cfg.temp_s, f"{name}: ventoinha voltou", clear_s=cfg.clear_s))
            if P is not None and not P.get("online"):
                out.append(Condition(f"{mid}:noshares", mid, f"{name}: pool não recebe shares",
                                     "A máquina está ligada, mas a pool não vê shares há mais de 10 min.",
                                     cfg.offline_s, f"{name}: pool voltou a receber shares",
                                     clear_s=cfg.clear_s))

    for p in snap["pools"]:
        scope = f"pool:{p['id']}"
        if p.get("ok") is None:
            unknown.add(scope)
        elif p.get("ok") is False:
            out.append(Condition(f"{scope}:down", scope, f"Pool {p['id']}: fora do ar",
                                 p.get("error") or "sem resposta", cfg.pool_s,
                                 f"Pool {p['id']}: voltou", clear_s=cfg.clear_s))
    return out, unknown


# ------------------------------------------------------------------ Telegram
class TelegramNotifier:
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str, api_base: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token, self.chat_id = token, str(chat_id)
        self.api_base = api_base or self.API
        self._transport = transport  # para testes

    async def send(self, text: str) -> bool:
        url = f"{self.api_base}/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as c:
                r = await c.post(url, json=payload)
            if r.status_code != 200:
                log.warning("Telegram recusou a mensagem (%s): %s", r.status_code,
                            self._mask(r.text[:200]))
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("falha ao enviar para o Telegram: %s", self._mask(str(e)))
            return False

    def _mask(self, text: str) -> str:
        return text.replace(self.token, "<token>") if self.token else text


def _duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{max(m, 1)} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}min"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


# ------------------------------------------------------------------ motor
class AlertEngine:
    def __init__(self, settings: AlertSettings, db=None, notifier: TelegramNotifier | None = None):
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.active: dict[str, AlertState] = {}
        self.recent: list[dict] = []
        if db is not None:
            for st in db.load_alert_states():
                self.active[st.key] = st
            self.recent = db.recent_alert_events(30)

    async def _db(self, method: str, *args) -> None:
        if self.db is not None:
            await asyncio.to_thread(getattr(self.db, method), *args)

    def _event(self, ts: float, st: AlertState, event: str) -> dict:
        ev = {"ts": ts, "key": st.key, "event": event, "kind": st.kind,
              "title": st.resolved_title if event == "resolved" else st.title, "detail": st.detail}
        self.recent.insert(0, ev)
        del self.recent[30:]
        return ev

    async def evaluate(self, snap: dict, now: float | None = None) -> list[str]:
        """Avalia o snapshot; envia e retorna as mensagens geradas."""
        now = time.time() if now is None else now
        conds, unknown = conditions(snap, self.settings)
        current = {c.key: c for c in conds}
        fired: list[AlertState] = []
        backlog: list[AlertState] = []   # já disparou só no painel; Telegram foi ligado depois
        resolved: list[tuple[AlertState, float]] = []

        for key, c in current.items():
            st = self.active.get(key)
            if st is None:
                st = AlertState(key, c.scope, c.kind, c.title, c.detail, c.resolved_title,
                                first_seen=now, clear_s=c.clear_s)
                self.active[key] = st
            st.title, st.detail, st.cleared_since = c.title, c.detail, None
            if st.notified is None and now - st.first_seen >= c.delay_s:
                fired.append(st)
            elif st.notified is not None and not st.sent_telegram and self.notifier is not None:
                backlog.append(st)
            await self._db("save_alert_state", st)

        for key, st in list(self.active.items()):
            if key in current or st.scope in unknown:
                continue                      # ainda ativo, ou sem dados para decidir
            if st.notified is None:           # oscilou antes de disparar: descarta em silêncio
                del self.active[key]
                await self._db("delete_alert_state", key)
                continue
            st.cleared_since = st.cleared_since or now
            if now - st.cleared_since >= st.clear_s:
                del self.active[key]
                resolved.append((st, now - st.first_seen))
                await self._db("delete_alert_state", key)
            else:
                await self._db("save_alert_state", st)

        if not fired and not resolved and not backlog:
            return []

        lines = []
        for st in fired + backlog:
            icon = "ℹ️" if st.kind == "info" else "🔴"
            since = f"\n<i>ativo desde {time.strftime('%d/%m %H:%M', time.localtime(st.first_seen))}</i>" \
                if st in backlog else ""
            lines.append(f"{icon} <b>{html.escape(st.title)}</b>\n{html.escape(st.detail)}{since}")
        for st, dur in resolved:
            icon = "⏹" if st.kind == "info" else "✅"
            lines.append(f"{icon} <b>{html.escape(st.resolved_title)}</b>\n(durou {_duration(dur)})")
        text = "\n\n".join(lines)

        sent = True
        if self.notifier is not None:
            sent = await self.notifier.send(text)
        if sent:
            for st in fired:
                st.notified = now
                st.sent_telegram = self.notifier is not None
                await self._db("save_alert_state", st)
                ev = self._event(now, st, "fired")
                await self._db("log_alert_event", ev)
            for st in backlog:
                st.sent_telegram = True
                await self._db("save_alert_state", st)
            for st, _ in resolved:
                ev = self._event(now, st, "resolved")
                await self._db("log_alert_event", ev)
        # se o envio falhou, os disparos ficam pendentes e são tentados de novo no próximo ciclo
        return lines

    # ------------------------------------------------------------------ ações do painel
    async def ack(self, key: str | None = None, acked: bool = True) -> int:
        """Fecha (ou reabre) alertas no aviso do painel. key=None = todos."""
        n = 0
        for st in self.active.values():
            if (key is None or st.key == key) and st.notified and st.acked != acked:
                st.acked = acked
                await self._db("save_alert_state", st)
                n += 1
        return n

    async def delete_event(self, ts: float, key: str, event: str) -> bool:
        before = len(self.recent)
        self.recent = [e for e in self.recent
                       if not (e["ts"] == ts and e["key"] == key and e["event"] == event)]
        await self._db("delete_alert_event", ts, key, event)
        return len(self.recent) < before

    async def clear_events(self) -> None:
        self.recent = []
        await self._db("clear_alert_events")

    def summary(self) -> dict:
        return {
            "telegram": self.notifier is not None,
            "active": [{"key": s.key, "kind": s.kind, "title": s.title, "detail": s.detail,
                        "since": s.first_seen, "acked": s.acked}
                       for s in self.active.values() if s.notified],
            "recent": self.recent[:15],
        }
