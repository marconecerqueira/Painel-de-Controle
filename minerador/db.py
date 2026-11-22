"""Armazena leituras no SQLite (base do histórico da Fase 2)."""

from __future__ import annotations

import asyncio
import json
import time
import sqlite3
import threading
from dataclasses import asdict

from .alerts import AlertState
from .models import LocalReading, PoolReading

SCHEMA = """
CREATE TABLE IF NOT EXISTS miner_readings (
    ts REAL NOT NULL,
    miner_id TEXT NOT NULL,
    online INTEGER NOT NULL,
    hashrate_ths REAL,
    power_w REAL,
    temp_max_c REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_miner_readings ON miner_readings (miner_id, ts);

CREATE TABLE IF NOT EXISTS pool_readings (
    ts REAL NOT NULL,
    pool_id TEXT NOT NULL,
    ok INTEGER NOT NULL,
    hashrate_ths REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pool_readings ON pool_readings (pool_id, ts);

-- uma linha por worker por consulta à pool: base dos gráficos por máquina
CREATE TABLE IF NOT EXISTS worker_readings (
    ts REAL NOT NULL,
    pool_id TEXT NOT NULL,
    worker TEXT NOT NULL,
    hashrate_ths REAL NOT NULL,
    online INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_worker_readings ON worker_readings (pool_id, worker, ts);

-- alertas em andamento (sobrevivem a reinícios, evitando reenvio)
CREATE TABLE IF NOT EXISTS alert_state (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
-- histórico de disparos/resoluções
CREATE TABLE IF NOT EXISTS alert_log (
    ts REAL NOT NULL,
    key TEXT NOT NULL,
    event TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alert_log ON alert_log (ts);
-- cards fechados no painel (ex.: aluguel que já acabou)
CREATE TABLE IF NOT EXISTS hidden_miners (
    miner_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
-- resultado de cada aluguel que terminou, congelado no fim (sobrevive ao × e ao config novo)
CREATE TABLE IF NOT EXISTS rental_results (
    miner_id TEXT NOT NULL,
    start REAL NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (miner_id, start)
);
-- recorde de share (maior dificuldade) de cada máquina e quando apareceu; não entra na limpeza
CREATE TABLE IF NOT EXISTS best_shares (
    miner_id TEXT PRIMARY KEY,
    value REAL NOT NULL,
    ts REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            # WAL + synchronous=NORMAL: menos escrita no cartão SD do Pi.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._backfill_workers()
            self._conn.commit()

    def _backfill_workers(self) -> None:
        """Bancos criados antes da tabela worker_readings: extrai os workers do JSON salvo."""
        if self._conn.execute("SELECT 1 FROM worker_readings LIMIT 1").fetchone():
            return
        rows = []
        for ts, pool_id, data in self._conn.execute(
                "SELECT ts, pool_id, data FROM pool_readings WHERE ok = 1"):
            for w in json.loads(data).get("workers") or []:
                rows.append((ts, pool_id, w["name"], w["hashrate_ths"], int(w["online"])))
        self._conn.executemany("INSERT INTO worker_readings VALUES (?,?,?,?,?)", rows)

    def _insert_miner(self, ts: float, miner_id: str, r: LocalReading) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO miner_readings VALUES (?,?,?,?,?,?,?)",
                (ts, miner_id, int(r.online), r.hashrate_ths, r.power_w, r.temp_max_c,
                 json.dumps(asdict(r))),
            )
            self._conn.commit()

    def _insert_pool(self, ts: float, pool_id: str, r: PoolReading) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO pool_readings VALUES (?,?,?,?,?)",
                (ts, pool_id, int(r.ok), r.hashrate_ths, json.dumps(asdict(r))),
            )
            if r.ok:
                self._conn.executemany(
                    "INSERT INTO worker_readings VALUES (?,?,?,?,?)",
                    [(ts, pool_id, w.name, w.hashrate_ths, int(w.online)) for w in r.workers],
                )
            self._conn.commit()

    async def add_miner_reading(self, ts: float, miner_id: str, r: LocalReading) -> None:
        await asyncio.to_thread(self._insert_miner, ts, miner_id, r)

    async def add_pool_reading(self, ts: float, pool_id: str, r: PoolReading) -> None:
        await asyncio.to_thread(self._insert_pool, ts, pool_id, r)

    def _prune(self, cutoff: float) -> int:
        with self._lock:
            n = self._conn.execute("DELETE FROM miner_readings WHERE ts < ?", (cutoff,)).rowcount
            n += self._conn.execute("DELETE FROM pool_readings WHERE ts < ?", (cutoff,)).rowcount
            n += self._conn.execute("DELETE FROM worker_readings WHERE ts < ?", (cutoff,)).rowcount
            n += self._conn.execute("DELETE FROM alert_log WHERE ts < ?", (cutoff,)).rowcount
            self._conn.commit()
            return n

    async def prune(self, cutoff: float) -> int:
        """Apaga leituras mais antigas que `cutoff` (epoch)."""
        return await asyncio.to_thread(self._prune, cutoff)

    # ------------------------------------------------------------------ alertas
    def load_alert_states(self) -> list[AlertState]:
        with self._lock:
            rows = self._conn.execute("SELECT data FROM alert_state").fetchall()
        return [AlertState(**json.loads(r[0])) for r in rows]

    def save_alert_state(self, st: AlertState) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?, ?)",
                               (st.key, json.dumps(asdict(st))))
            self._conn.commit()

    def delete_alert_state(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM alert_state WHERE key = ?", (key,))
            self._conn.commit()

    def log_alert_event(self, ev: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO alert_log VALUES (?,?,?,?)",
                               (ev["ts"], ev["key"], ev["event"], json.dumps(ev)))
            self._conn.commit()

    def recent_alert_events(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM alert_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete_alert_event(self, ts: float, key: str, event: str) -> int:
        with self._lock:
            n = self._conn.execute("DELETE FROM alert_log WHERE ts = ? AND key = ? AND event = ?",
                                   (ts, key, event)).rowcount
            self._conn.commit()
        return n

    def clear_alert_events(self) -> int:
        with self._lock:
            n = self._conn.execute("DELETE FROM alert_log").rowcount
            self._conn.commit()
        return n

    # ------------------------------------------------------------------ cards ocultos
    def hidden_miners(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT miner_id FROM hidden_miners")}

    def set_miner_hidden(self, miner_id: str, hidden: bool) -> None:
        with self._lock:
            if hidden:
                self._conn.execute("INSERT OR REPLACE INTO hidden_miners VALUES (?, ?)", (miner_id, time.time()))
            else:
                self._conn.execute("DELETE FROM hidden_miners WHERE miner_id = ?", (miner_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ aluguéis terminados
    def save_rental_result(self, miner_id: str, start: float, data: dict) -> bool:
        """Grava o resultado de um aluguel terminado só na 1ª vez (fica congelado). True se gravou."""
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO rental_results VALUES (?, ?, ?)",
                                     (miner_id, start, json.dumps(data)))
            self._conn.commit()
            return cur.rowcount > 0

    def rental_results(self, limit: int = 20) -> list[dict]:
        """Aluguéis terminados, do mais recente para o mais antigo."""
        with self._lock:
            rows = self._conn.execute("SELECT data FROM rental_results ORDER BY start DESC LIMIT ?", (limit,))
            # "ignored": aluguel que o usuário decidiu deixar fora da lista (a linha só impede regravar)
            return [d for d in (json.loads(r[0]) for r in rows) if not d.get("ignored")]

    # ------------------------------------------------------------------ recorde de share
    def best_shares(self) -> dict[str, tuple[float, float]]:
        """id da máquina -> (dificuldade, quando apareceu)."""
        with self._lock:
            return {r[0]: (r[1], r[2]) for r in self._conn.execute("SELECT miner_id, value, ts FROM best_shares")}

    def save_best_share(self, miner_id: str, value: float, ts: float) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO best_shares VALUES (?, ?, ?)", (miner_id, value, ts))
            self._conn.commit()

    def best_share_from_history(self, miner_id: str | None, pool_id: str | None,
                                worker: str | None) -> tuple[float, float] | None:
        """Maior dificuldade já gravada nas leituras guardadas e o 1º momento em que apareceu.

        Usado uma vez por máquina, para o recorde não nascer com a data de hoje.
        """
        rows: list = []
        with self._lock:
            if miner_id:
                rows += self._conn.execute(
                    "SELECT ts, json_extract(data, '$.best_diff') FROM miner_readings "
                    "WHERE miner_id = ? AND online = 1", (miner_id,)).fetchall()
            if pool_id and worker:
                rows += self._conn.execute(
                    "SELECT p.ts, json_extract(w.value, '$.best_diff') FROM pool_readings p, "
                    "json_each(p.data, '$.workers') w WHERE p.pool_id = ? AND p.ok = 1 "
                    "AND json_extract(w.value, '$.name') = ?", (pool_id, worker)).fetchall()
        best = None
        for ts, v in sorted(rows):
            if v is not None and (best is None or v > best[0]):
                best = (v, ts)
        return best

    # ------------------------------------------------------------------ histórico
    def _bucketed(self, sql: str, args: tuple, bucket_s: int) -> list[list[float]]:
        with self._lock:
            rows = self._conn.execute(sql, (bucket_s, bucket_s, *args)).fetchall()
        return [[b + bucket_s / 2, v] for b, v in rows]   # carimbo no meio do intervalo

    def _miner_history(self, miner_id: str, since: float, bucket_s: int):
        return self._bucketed(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS b, AVG(hashrate_ths) FROM miner_readings "
            "WHERE miner_id = ? AND ts >= ? AND online = 1 AND hashrate_ths IS NOT NULL "
            "GROUP BY b ORDER BY b", (miner_id, since), bucket_s)

    def _worker_history(self, pool_id: str, worker: str, since: float, bucket_s: int):
        # worker offline conta como 0 (ele estava na lista, mas sem shares)
        return self._bucketed(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS b, AVG(CASE WHEN online = 1 THEN hashrate_ths ELSE 0 END) "
            "FROM worker_readings WHERE pool_id = ? AND worker = ? AND ts >= ? "
            "GROUP BY b ORDER BY b", (pool_id, worker, since), bucket_s)

    def _worker_run(self, pool_id: str, worker: str, since: float, gap_s: float):
        """(início, fim) da sequência mais recente de leituras online sem buracos > gap_s."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM worker_readings WHERE pool_id = ? AND worker = ? AND online = 1 AND ts >= ? "
                "ORDER BY ts DESC", (pool_id, worker, since)).fetchall()
        if not rows:
            return None
        end = start = rows[0][0]
        for (ts,) in rows[1:]:
            if start - ts > gap_s:
                break
            start = ts
        return start, end

    def _worker_avg(self, pool_id: str, worker: str, since: float, until: float):
        """(média TH/s, nº de leituras) com o worker online no intervalo."""
        with self._lock:
            row = self._conn.execute(
                "SELECT AVG(hashrate_ths), COUNT(*) FROM worker_readings WHERE pool_id = ? AND worker = ? "
                "AND online = 1 AND ts >= ? AND ts <= ?", (pool_id, worker, since, until)).fetchone()
        return (row[0], row[1]) if row and row[1] else (None, 0)

    def _worker_mean(self, pool_id: str, worker: str, since: float):
        """(média TH/s, nº de leituras) desde `since`, com o worker offline contando como 0."""
        with self._lock:
            row = self._conn.execute(
                "SELECT AVG(CASE WHEN online = 1 THEN hashrate_ths ELSE 0 END), COUNT(*) FROM worker_readings "
                "WHERE pool_id = ? AND worker = ? AND ts >= ?", (pool_id, worker, since)).fetchone()
        return (row[0], row[1]) if row and row[1] else (None, 0)

    def _miner_means(self, miner_id: str, since: float):
        """(média TH/s com offline = 0, média W das leituras com potência, nº de leituras)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT AVG(CASE WHEN online = 1 THEN COALESCE(hashrate_ths, 0) ELSE 0 END), "
                "AVG(CASE WHEN online = 1 THEN power_w END), COUNT(*) FROM miner_readings "
                "WHERE miner_id = ? AND ts >= ?", (miner_id, since)).fetchone()
        return (row[0], row[1], row[2]) if row and row[2] else (None, None, 0)

    async def worker_mean(self, pool_id: str, worker: str, since: float):
        return await asyncio.to_thread(self._worker_mean, pool_id, worker, since)

    async def miner_means(self, miner_id: str, since: float):
        return await asyncio.to_thread(self._miner_means, miner_id, since)

    async def worker_run(self, pool_id: str, worker: str, since: float, gap_s: float):
        return await asyncio.to_thread(self._worker_run, pool_id, worker, since, gap_s)

    async def worker_avg(self, pool_id: str, worker: str, since: float, until: float):
        return await asyncio.to_thread(self._worker_avg, pool_id, worker, since, until)

    async def miner_history(self, miner_id: str, since: float, bucket_s: int):
        return await asyncio.to_thread(self._miner_history, miner_id, since, bucket_s)

    async def worker_history(self, pool_id: str, worker: str, since: float, bucket_s: int):
        return await asyncio.to_thread(self._worker_history, pool_id, worker, since, bucket_s)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
