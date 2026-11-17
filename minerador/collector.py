"""Laço de coleta: consulta máquinas e pools, guarda no SQLite e monta o snapshot do painel."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict

import httpx

from .config import Config, MinerConfig
from .alerts import AlertEngine
from .db import Database
from .finance import Market, compute_rental
from .models import LocalReading, PoolReading, PoolWorker
from .sources import axeos, defisurfers, hashcommander, luxos, parasite

log = logging.getLogger("minerador.collector")

# período -> (duração s, tamanho do intervalo s, parâmetro range da defisurfers)
HISTORY_RANGES = {
    "24h": (86400, 600, "1d"),
    "3d": (3 * 86400, 1800, "3d"),
    "7d": (7 * 86400, 3600, "7d"),
    "30d": (30 * 86400, 4 * 3600, "1m"),
}
POOL_CHART_TTL_S = 300
POOL_SIDE_TYPES = {"pool_only", "rental"}  # sem API local: tudo vem da pool


def worker_from_user(user: str | None) -> str | None:
    """'bc1qxyz.s19' -> 's19'."""
    if not user or "." not in user:
        return None
    return user.split(".", 1)[1] or None


def short_address(addr: str) -> str:
    return addr if len(addr) <= 16 else f"{addr[:8]}…{addr[-6:]}"


class Collector:
    def __init__(self, cfg: Config, db: Database | None = None, alerts: AlertEngine | None = None):
        self.cfg = cfg
        self.db = db
        self.alerts = alerts
        self._local_ready = self._pools_ready = False
        self.local: dict[str, tuple[float, LocalReading]] = {}
        self.pools: dict[str, tuple[float, PoolReading]] = {}
        # Última leitura BOA de cada pool: uma falha passageira da API não deve
        # derrubar o status dos workers (evita alarmes falsos).
        self.pools_last_ok: dict[str, tuple[float, PoolReading]] = {}
        self._client: httpx.AsyncClient | None = None
        self._chart_cache: dict[tuple[str, str], tuple[float, list]] = {}
        self.market = Market(cfg.mempool_url)
        self.rentals: dict[str, dict] = {}                 # id da máquina -> resumo financeiro
        self._worker_mode: dict[tuple[str, str], str] = {}  # (pool, worker) -> "solo" / "PPLNS"
        self.hidden: set[str] = db.hidden_miners() if db else set()   # cards fechados no painel

    @property
    def pool_grace_s(self) -> float:
        return max(300.0, 3 * self.cfg.pool_interval)

    def _fresh_pool(self, pool_id: str | None, now: float) -> PoolReading | None:
        if not pool_id or pool_id not in self.pools_last_ok:
            return None
        ts, reading = self.pools_last_ok[pool_id]
        return reading if now - ts <= self.pool_grace_s else None

    # ------------------------------------------------------------------ coleta
    async def _poll_miner(self, m: MinerConfig) -> None:
        assert self._client is not None
        if m.type == "luxos":
            reading = await luxos.fetch(m.host, m.port or 4028)
        elif m.type == "axeos":
            reading = await axeos.fetch(self._client, m.host, m.port or 80)
        else:
            return
        ts = time.time()
        self.local[m.id] = (ts, reading)
        if self.db:
            await self.db.add_miner_reading(ts, m.id, reading)

    async def _poll_pool(self, pool_id: str) -> None:
        assert self._client is not None
        p = self.cfg.pools[pool_id]
        if p.type == "defisurfers":
            reading = await defisurfers.fetch(self._client, p.base_url, p.address)
        elif p.type == "hashcommander":
            reading = await hashcommander.fetch(self._client, p.base_url, p.address)
        else:
            reading = await parasite.fetch(self._client, p.base_url, p.address)
        ts = time.time()
        self.pools[pool_id] = (ts, reading)
        if reading.ok:
            self.pools_last_ok[pool_id] = (ts, reading)
            for w in reading.workers:
                if w.mode:
                    self._worker_mode[(pool_id, w.name)] = w.mode
        else:
            log.warning("pool %s: %s", pool_id, reading.error)
        if self.db:
            await self.db.add_pool_reading(ts, pool_id, reading)

    async def poll_local_once(self) -> None:
        await asyncio.gather(*(self._safe(self._poll_miner(m), m.id)
                               for m in self.cfg.miners if m.type not in POOL_SIDE_TYPES))
        self._local_ready = True

    async def poll_pools_once(self) -> None:
        await asyncio.gather(*(self._safe(self._poll_pool(pid), pid) for pid in self.cfg.pools))
        first = not self._pools_ready
        self._pools_ready = True
        await self._reopen_active()
        if first:   # financeiro já na abertura do painel, sem esperar o laço de 1 min
            await self._safe(self.finance_once(), "financeiro")

    # ------------------------------------------------------------------ cards fechados
    ACTIVE = ("online", "warning")

    async def _reopen_active(self) -> None:
        """Aluguel fechado que voltou a funcionar reaparece sozinho."""
        for m in self.snapshot()["miners"]:
            if m["id"] in self.hidden and m["status"] in self.ACTIVE:
                await self.set_hidden(m["id"], False)

    async def set_hidden(self, miner_id: str, hidden: bool) -> None:
        """Fecha/reabre o card. Só aluguéis que não estão funcionando podem ser fechados."""
        m = next((x for x in self.snapshot()["miners"] if x["id"] == miner_id), None)
        if m is None:
            raise KeyError(miner_id)
        if hidden and (m["type"] != "rental" or m["status"] in self.ACTIVE):
            raise ValueError("só dá para fechar aluguéis que já não estão em vigor")
        (self.hidden.add if hidden else self.hidden.discard)(miner_id)
        if self.db:
            await asyncio.to_thread(self.db.set_miner_hidden, miner_id, hidden)

    async def evaluate_alerts_once(self) -> None:
        # Só avalia com dados frescos de todas as fontes: evita "resolvido" falso logo após reiniciar.
        if self.alerts is None or not (self._local_ready and self._pools_ready):
            return
        await self.alerts.evaluate(self.snapshot())

    @staticmethod
    async def _safe(coro, label: str) -> None:
        try:
            await coro
        except Exception:  # um erro numa fonte nunca derruba o laço
            log.exception("erro inesperado ao consultar %s", label)

    async def _loop(self, fn, interval: float) -> None:
        while True:
            started = time.monotonic()
            await fn()
            await asyncio.sleep(max(1.0, interval - (time.monotonic() - started)))

    async def finance_once(self) -> None:
        """Recalcula o financeiro dos aluguéis com cost_btc e rental_hours no config."""
        rentals = [m for m in self.cfg.miners if m.type == "rental" and m.cost_btc and m.rental_hours]
        # espera a 1ª leitura das pools: é ela que diz o modo (solo/PPLNS) de cada worker
        if not rentals or self._client is None or self.db is None or not self._pools_ready:
            return
        now = time.time()
        btc = await self.market.btc(self._client)
        gap = max(900.0, 3 * self.cfg.pool_interval)
        for m in rentals:
            pool = self.cfg.pools[m.pool]
            coin = (await self.market.bch_hashcommander(self._client, pool.base_url)
                    if pool.type == "hashcommander" else btc)
            run = await self.db.worker_run(m.pool, m.worker, now - 7 * 86400, gap)
            start, source = m.rental_start, "config"
            if start is None and run is not None:
                start, source = run[0], "detectado"
            avg, n = (None, 0)
            if start is not None:
                avg, n = await self.db.worker_avg(m.pool, m.worker, start,
                                                  min(now, start + m.rental_hours * 3600))
            good = self._fresh_pool(m.pool, now)
            cur = next((w for w in (good.workers if good else []) if w.name == m.worker and w.online), None)
            # modo desconhecido (ex.: painel reiniciado depois do fim do aluguel) fica None: sem chute
            mode = self._worker_mode.get((m.pool, m.worker)) or ("PPLNS" if pool.type == "defisurfers" else None)
            self.rentals[m.id] = compute_rental(
                cost_btc=m.cost_btc, hours=m.rental_hours, start=start, start_source=source,
                newer_run_start=run[0] if run and m.rental_start is not None else None,
                avg_ths=avg, avg_samples=n, current_ths=cur.hashrate_ths if cur else None,
                mode=mode, coin_market=coin, btc_market=btc, now=now)

    async def prune_once(self) -> None:
        if self.db and self.cfg.retention_days > 0:
            n = await self.db.prune(time.time() - self.cfg.retention_days * 86400)
            if n:
                log.info("limpeza: %d leituras antigas removidas", n)

    async def run(self) -> None:
        async with httpx.AsyncClient(headers={"User-Agent": "minerador/0.1"},
                                     follow_redirects=True) as client:
            self._client = client
            try:
                await asyncio.gather(
                    self._loop(self.poll_local_once, self.cfg.local_interval),
                    self._loop(self.poll_pools_once, self.cfg.pool_interval),
                    self._loop(lambda: self._safe(self.prune_once(), "limpeza"), 3600),
                    self._loop(lambda: self._safe(self.finance_once(), "financeiro"), 60),
                    self._loop(lambda: self._safe(self.evaluate_alerts_once(), "alertas"),
                               min(15.0, self.cfg.local_interval)),
                )
            finally:
                self._client = None

    # ------------------------------------------------------------------ snapshot
    def _pool_worker(self, m: MinerConfig, local: LocalReading | None,
                     pr: PoolReading | None) -> PoolWorker | None:
        name = m.worker or worker_from_user(local.pool_user if local else None)
        if not name or pr is None:
            return None
        return next((w for w in pr.workers if w.name == name), None)

    def _pool_ok(self, pool_id: str | None, now: float) -> bool | None:
        """None = ainda não consultada; True = há leitura boa recente; False = fora do ar."""
        if not pool_id or pool_id not in self.pools:
            return None
        return self._fresh_pool(pool_id, now) is not None

    def _status(self, m: MinerConfig, local: LocalReading | None, pool_worker: PoolWorker | None,
                pool_ok: bool | None) -> tuple[str, list[str]]:
        """Retorna (status, motivos). status: online | warning | offline | idle | unknown.

        idle = aluguel sem contrato ativo (worker ausente é o esperado, não é falha).
        """
        reasons: list[str] = []
        ratio = self.cfg.alerts.low_hashrate_ratio
        if m.type in POOL_SIDE_TYPES:
            if pool_ok is None:
                return "unknown", ["aguardando dados da pool"]
            if not pool_ok:
                return "unknown", ["pool indisponível"]
            if pool_worker is None or not pool_worker.online:
                if m.type == "rental":
                    return "idle", ["sem aluguel ativo"]
                if pool_worker is None:
                    return "offline", ["worker não aparece na pool"]
                return "offline", ["sem shares há mais de 10 min"]
            # Hashrate na pool é uma estimativa por shares: oscila, por isso a margem de 70%.
            if m.expected_ths and pool_worker.hashrate_ths < m.expected_ths * ratio:
                what = "contratado" if m.type == "rental" else "esperado"
                reasons.append(f"pool vê {pool_worker.hashrate_ths:.1f} TH/s, abaixo de "
                               f"{ratio:.0%} do {what} ({m.expected_ths:g} TH/s)")
            return ("warning" if reasons else "online"), reasons

        if local is None:
            return "unknown", ["aguardando primeira leitura"]
        if not local.online:
            return "offline", [local.error or "sem resposta"]

        reasons.extend(local.warnings)
        if m.temp_max_c and local.temp_max_c and local.temp_max_c >= m.temp_max_c:
            reasons.append(f"temperatura {local.temp_max_c:.0f}°C ≥ limite {m.temp_max_c:.0f}°C")
        if m.vr_temp_max_c and local.temp_vr_c and local.temp_vr_c >= m.vr_temp_max_c:
            reasons.append(f"VR {local.temp_vr_c:.0f}°C ≥ limite {m.vr_temp_max_c:.0f}°C")
        expected = m.expected_ths or local.expected_ths
        current = local.hashrate_avg_ths or local.hashrate_ths
        if expected and current is not None and current < expected * ratio:
            reasons.append(f"hashrate {current:.2f} TH/s abaixo de {ratio:.0%} do esperado")
        if pool_ok and pool_worker is not None and not pool_worker.online:
            reasons.append("pool não recebe shares há mais de 10 min")
        return ("warning" if reasons else "online"), reasons

    def snapshot(self) -> dict:
        now = time.time()
        miners = []
        tot_hash = tot_power = 0.0
        online = total = 0
        for m in self.cfg.miners:
            ts, local = self.local.get(m.id, (None, None))
            pw = self._pool_worker(m, local, self._fresh_pool(m.pool, now))
            pool_ok = self._pool_ok(m.pool, now)
            status, reasons = self._status(m, local, pw, pool_ok)
            if status != "idle":          # aluguel sem contrato não conta como máquina
                total += 1
            if status in ("online", "warning"):
                online += 1
            if local and local.online:
                tot_hash += local.hashrate_ths or 0
                tot_power += local.power_w or 0
            elif m.type in POOL_SIDE_TYPES and pw and pw.online:
                tot_hash += pw.hashrate_ths
            miners.append({
                "id": m.id, "name": m.name, "type": m.type, "pool": m.pool,
                "coin": "BCH" if m.pool and self.cfg.pools[m.pool].type == "hashcommander" else "BTC",
                "worker": m.worker or worker_from_user(local.pool_user if local else None),
                "status": status, "reasons": reasons,
                "limits": {"temp_max_c": m.temp_max_c, "vr_temp_max_c": m.vr_temp_max_c,
                           "expected_ths": m.expected_ths},
                "local": asdict(local) if local else None, "local_at": ts,
                "pool_worker": asdict(pw) if pw else None,
                "rental": self.rentals.get(m.id),
                "hidden": m.id in self.hidden,
            })

        pools = []
        for pid, p in self.cfg.pools.items():
            ts, pr = self.pools.get(pid, (None, None))
            entry = {"id": pid, "type": p.type, "address": short_address(p.address),
                     "url": p.base_url, "at": ts, "ok": None}
            if pr is not None:
                good = self._fresh_pool(pid, now)
                if not pr.ok and good is not None:
                    # falha passageira: mostra os últimos dados bons + o erro atual
                    good_ts = self.pools_last_ok[pid][0]
                    entry.update(asdict(good))
                    entry.update(at=good_ts, ok=True, error=f"última consulta falhou ({pr.error})")
                else:
                    entry.update(asdict(pr))
            pools.append(entry)

        return {
            "generated_at": now,
            "miners": miners,
            "pools": pools,
            "alerts": self.alerts.summary() if self.alerts else None,
            "totals": {"hashrate_ths": tot_hash, "power_w": tot_power,
                       "online": online, "total": total},
        }

    # ------------------------------------------------------------------ histórico
    async def _pool_total(self, pool_id: str, range_: str, since: float, bucket_s: int) -> list | None:
        """Histórico do endereço direto da pool (só defisurfers expõe isso de forma confiável)."""
        p = self.cfg.pools[pool_id]
        if p.type != "defisurfers":
            return None
        key = (pool_id, range_)
        cached = self._chart_cache.get(key)
        if cached and time.time() - cached[0] < POOL_CHART_TTL_S:
            raw = cached[1]
        else:
            try:
                if self._client is not None:
                    raw = await defisurfers.fetch_chart(self._client, p.base_url, p.address, range_)
                else:
                    async with httpx.AsyncClient(headers={"User-Agent": "minerador/0.1"}) as c:
                        raw = await defisurfers.fetch_chart(c, p.base_url, p.address, range_)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("histórico da pool %s indisponível: %s", pool_id, e)
                return cached[1] if cached else None
            self._chart_cache[key] = (time.time(), raw)
        buckets: dict[int, list[float]] = {}
        for ts, v in raw:
            if ts >= since:
                buckets.setdefault(int(ts // bucket_s), []).append(v)
        return [[b * bucket_s + bucket_s / 2, sum(v) / len(v)] for b, v in sorted(buckets.items())]

    async def history(self, range_key: str) -> dict:
        if range_key not in HISTORY_RANGES:
            raise ValueError(f"período inválido: {range_key}")
        span, bucket_s, pool_range = HISTORY_RANGES[range_key]
        now = time.time()
        since = now - span

        series = []
        for m in self.cfg.miners:
            points: list = []
            source = None
            if self.db is not None:
                if m.type in POOL_SIDE_TYPES:
                    points = await self.db.worker_history(m.pool, m.worker, since, bucket_s)
                    source = "pool"
                else:
                    points = await self.db.miner_history(m.id, since, bucket_s)
                    source = "máquina"
            series.append({"id": m.id, "name": m.name, "type": m.type, "source": source,
                           "points": [[round(t), round(v, 4)] for t, v in points]})

        totals = []
        for pid in self.cfg.pools:
            pts = await self._pool_total(pid, pool_range, since, bucket_s)
            if pts is not None:
                totals.append({"id": pid, "name": f"Total na pool ({pid})",
                               "points": [[round(t), round(v, 4)] for t, v in pts]})

        return {"range": range_key, "since": since, "until": now, "bucket_s": bucket_s,
                "series": series, "pool_totals": totals}
