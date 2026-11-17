"""Simula LuxOS (TCP), AxeOS e as APIs das pools para testar o painel sem hardware.

Uso:  python tools/simulador.py            (LuxOS em :14028, HTTP em :18081)
Depois: python -m minerador --config config.demo.toml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

START = time.time()
ADDR = "bc1qdemo0000000000000000000000000000000000"


def wobble(base: float, pct: float = 0.03) -> float:
    return base * (1 + random.uniform(-pct, pct))


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ LuxOS (S19j XP)
def luxos_response(cmd: str) -> dict:
    ok = [{"STATUS": "S", "When": int(time.time()), "Code": 11, "Msg": "Summary", "Description": "LUXminer 2026.10.1"}]
    up = int(time.time() - START) + 3 * 86400
    if cmd == "summary":
        return {"STATUS": ok, "SUMMARY": [{
            "Elapsed": up, "GHS 5s": wobble(141000), "GHS 30m": wobble(140500, 0.01),
            "GHS av": 140800.0, "Accepted": 48213 + up // 10, "Rejected": 31, "Stale": 2,
            "Hardware Errors": 120, "Device Hardware%": 0.02, "Best Share": 2.1e11}], "id": 1}
    if cmd == "power":
        return {"STATUS": ok, "POWER": [{"Watts": round(wobble(3010, 0.01)), "PSU": True}], "id": 1}
    if cmd == "temps":
        return {"STATUS": ok, "TEMPS": [
            {"ID": i, "TEMP": i, "TopLeft": round(wobble(62)), "TopRight": round(wobble(66)),
             "BottomLeft": round(wobble(58)), "BottomRight": round(wobble(64))} for i in range(3)], "id": 1}
    if cmd == "fans":
        return {"STATUS": ok, "FANS": [{"ID": i, "RPM": round(wobble(4200)), "Speed": 70} for i in range(4)], "id": 1}
    if cmd == "pools":
        return {"STATUS": ok, "POOLS": [{
            "POOL": 0, "URL": "stratum+tcp://pool.defisurfers.xyz:2020", "User": f"{ADDR}.s19",
            "Status": "Alive", "Stratum Active": True, "Accepted": 48213, "Rejected": 31}], "id": 1}
    if cmd == "version":
        return {"STATUS": ok, "VERSION": [{"LUXminer": "2026.10.1", "API": "3.7", "Type": "Antminer S19j XP"}], "id": 1}
    return {"STATUS": [{"STATUS": "E", "Code": 14, "Msg": "Invalid command"}], "id": 1}


async def handle_luxos(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(4096), 5)
        cmd = json.loads(raw.decode()).get("command", "")
        writer.write(json.dumps(luxos_response(cmd)).encode() + b"\x00")
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


# ------------------------------------------------------------------ HTTP: AxeOS + pools
app = FastAPI()


@app.get("/api/system/info")
def axeos_info():
    up = int(time.time() - START) + 7200
    # temperatura sobe e desce devagar para o medidor mexer
    temp = 58 + 6 * math.sin(time.time() / 120)
    return {
        "hashRate": wobble(1150), "hashRate_1h": 1130.0, "expectedHashrate": 1190.0,
        "temp": round(temp, 1), "vrTemp": round(temp + 9, 1), "power": round(wobble(18.5), 2),
        "fanrpm": 4100, "fanspeed": 55, "sharesAccepted": 9120 + up // 15, "sharesRejected": 4,
        "bestDiff": 1.92e9, "uptimeSeconds": up, "stratumURL": "pool.defisurfers.xyz",
        "stratumPort": 2018, "stratumUser": f"{ADDR}.bitaxe", "ASICModel": "BM1370",
        "boardVersion": "601", "axeOSVersion": "v2.15.4", "overheat_mode": 0,
        "isUsingFallbackStratum": 0,
    }


@app.get("/defi/api/client/{addr}")
def defi_client(addr: str):
    now = time.time()
    return {"bestDifficulty": 2.1e11, "workersCount": 2, "totalHashrate": wobble(142e12),
            "workers": [
                {"sessionId": "a1", "name": "s19", "hashRate": wobble(141e12), "bestDifficulty": "210000000000.00",
                 "startTime": iso(now - 86400), "lastSeen": iso(now - random.randint(1, 40))},
                {"sessionId": "b2", "name": "bitaxe", "hashRate": wobble(1.1e12), "bestDifficulty": "1920102528.00",
                 "startTime": iso(now - 7200), "lastSeen": iso(now - random.randint(1, 40))},
            ]}


@app.get("/defi/api/client/{addr}/chart")
def defi_chart(addr: str):
    now = time.time()
    return [{"label": iso(now - (i * 600)), "data": wobble(141.5e12)} for i in range(144, 0, -1)]


@app.get("/defi/api/pplns/{addr}")
def defi_pplns(addr: str):
    return {"balanceSats": 0, "totalPaidSats": 0, "currentWindowShares": 3.7e10,
            "currentWindowPercent": 13.9, "balanceLabel": "zero"}


@app.get("/defi/api/pool")
def defi_pool():
    # um bloco achado pelo Bitaxe da demonstração (para ver o destaque no painel)
    return {"totalHashRate": 1.02e18, "blockHeight": 975900, "totalMiners": 97, "fee": 0,
            "blocksFound": [{"height": 975850, "minerAddress": ADDR, "worker": "bitaxe", "sessionId": "b2"}]}


@app.get("/parasite/api/user/{addr}")
def parasite_user(addr: str):
    now = int(time.time())
    return {"hashrate": 52000, "workers": 1, "lastSubmission": "12s ago", "bestDifficulty": "1.2M",
            "uptime": "40d 2h", "workerData": [
                {"id": f"{addr}.nerdminer", "name": "nerdminer", "hashrate": "52000",
                 "bestDifficulty": "1203344", "lastSubmission": str(now - random.randint(5, 90)), "uptime": "N/A"}]}


@app.get("/parasite/api/user/{addr}/historical")
def parasite_hist(addr: str):
    return JSONResponse([])


# ------------------------------------------------------------------ Hash Commander (BCH)
BCH_ADDR = "qdemo00000000000000000000000000000000000000"


def _hc_id(addr: str) -> str:
    import hashlib
    return hashlib.sha256(addr.lower().encode()).hexdigest()[:16]


@app.get("/hc/api/miners")
def hc_miners():
    return {"updated": int(time.time()), "pool_hashrate": wobble(260e12),
            "miners": [
                {"id": "265c7349fa86e6c1", "address": "qzjp86…c70t", "share_pct": 77.9, "receives_bch": 1.62,
                 "hashrate": wobble(6.2e12), "online": True, "best_share": 26123830,
                 "machines": [{"name": "s9", "hashrate": wobble(6.2e12), "best_share": 26123830}]},
                {"id": _hc_id(BCH_ADDR), "address": "qdemo0…0000", "share_pct": 0.1, "receives_bch": 0.00207,
                 "hashrate": wobble(3.6e12), "online": True, "best_share": 2129651,
                 "machines": [{"name": "aluguel-pplns", "hashrate": wobble(3.6e12), "best_share": 2129651}]}],
            "solo_miners": [
                {"id": _hc_id(BCH_ADDR), "address": "qdemo0…0000", "hashrate": wobble(222e12), "best_share": 13319007,
                 "machines": [{"name": "aluguel-solo", "hashrate": wobble(222e12), "best_share": 13319007}]}]}


@app.get("/hc/api/pool")
def hc_pool():
    return {"totalHashRate": wobble(260e12), "blockHeight": 977674, "totalMiners": 5, "fee": 1.5, "blocksFound": []}


@app.get("/hc/api/stats")
def hc_stats():
    return {"pools": {"bitcoincash": {"symbol": "BCH", "networkDifficulty": 495226832114.3363}}}


@app.get("/hc/estado.json")
def hc_estado():
    return {"preco_bch_eur": 190.18, "dificuldade_rede": 495216621602.6952, "recompensa_bch": 3.12766642,
            "taxa_pct": 1.5, "taxa_solo_pct": 2.0}


@app.get("/mempool/api/v1/prices")
def mem_prices():
    return {"time": int(time.time()), "USD": 65251, "EUR": 59332}


@app.get("/hc/api/blocks")
def hc_blocks():
    return {"updated": int(time.time()), "blocks": [
        {"height": 977600, "hash": "00" * 32, "ts": int(time.time()) - 86400, "reward_bch": 3.1258,
         "worker": "aluguel-solo", "finder_id": _hc_id(BCH_ADDR)}]}


# ------------------------------------------------------------------ mempool.space
POOLS = [("foundryusa", "Foundry USA", 14558), ("antpool", "AntPool", 9570), ("f2pool", "F2Pool", 6836),
         ("solock", "Solo CK", 14), ("publicpool", "Public Pool", 6), ("parasite", "Parasite", 5)]


def mem_block(height: int, slug: str, name: str, ts: float, addr: str = "bc1qoutro") -> dict:
    return {"id": f"{height:064x}", "height": height, "timestamp": int(ts), "tx_count": 3000 + height % 2000,
            "difficulty": 134823837323272.7,
            "extras": {"reward": 312_500_000 + height % 3_000_000, "totalFees": height % 3_000_000,
                       "pool": {"id": 1, "name": name, "slug": slug}, "coinbaseAddresses": [addr]}}


def tip() -> int:
    return 975900 + int((time.time() - START) // 600)


@app.get("/mempool/api/v1/blocks")
def mem_recent():
    h, now = tip(), time.time()
    return [mem_block(h - i, *POOLS[(h - i) % 3][:2], now - i * 600 - 120) for i in range(15)]


@app.get("/mempool/api/v1/mining/pool/{slug}/blocks")
def mem_pool_blocks(slug: str):
    name = next((n for s, n, _ in POOLS if s == slug), None)
    if name is None:
        return []
    gap = {"solock": 4000, "publicpool": 9000, "parasite": 12000}.get(slug, 3)
    return [mem_block(975800 - i * gap, slug, name, time.time() - 3600 - i * gap * 600) for i in range(5)]


@app.get("/mempool/api/v1/mining/pools/{period}")
def mem_pools(period: str):
    return {"pools": [{"slug": s, "name": n, "blockCount": c} for s, n, c in POOLS]}


@app.get("/mempool/api/block-height/{height}")
def mem_height(height: int):
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(f"{height:064x}")


@app.get("/mempool/api/v1/block/{block_hash}")
def mem_block_by_hash(block_hash: str):
    h = int(block_hash, 16)
    return mem_block(h, "unknown", "Unknown", time.time() - (tip() - h) * 600, ADDR)


async def main(lux_port: int, http_port: int) -> None:
    server = await asyncio.start_server(handle_luxos, "127.0.0.1", lux_port)
    print(f"LuxOS simulado em 127.0.0.1:{lux_port} · AxeOS/pools em http://127.0.0.1:{http_port}")
    web = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=http_port, log_level="warning"))
    async with server:
        await asyncio.gather(server.serve_forever(), web.serve())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--luxos-port", type=int, default=14028)
    ap.add_argument("--http-port", type=int, default=18081)
    a = ap.parse_args()
    asyncio.run(main(a.luxos_port, a.http_port))
