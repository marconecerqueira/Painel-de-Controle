from minerador.models import parse_number
from minerador.sources import axeos, defisurfers, luxos, parasite

NOW = 1_794_702_700.0  # epoch fixo para os testes


def test_parse_number_suffixes():
    assert parse_number("4.29G") == 4.29e9
    assert parse_number("400.96T") == 400.96e12
    assert parse_number("1920102528.00") == 1920102528.0
    assert parse_number(12) == 12.0
    assert parse_number("abc") is None
    assert parse_number(None) is None
    assert parse_number(True) is None


# ------------------------------------------------------------------ LuxOS
def test_luxos_parse_full():
    ok = [{"STATUS": "S"}]
    r = luxos.parse(
        summary={"STATUS": ok, "SUMMARY": [{"GHS 5s": 141000.0, "GHS av": 140000.0, "Elapsed": 3600,
                                            "Accepted": 100, "Rejected": 2, "Device Hardware%": 0.01}]},
        pools={"STATUS": ok, "POOLS": [
            {"URL": "stratum+tcp://backup:3333", "User": "x.y", "Status": "Alive", "Stratum Active": False},
            {"URL": "stratum+tcp://pool.defisurfers.xyz:2020", "User": "bc1qabc.s19", "Status": "Alive",
             "Stratum Active": True}]},
        power={"STATUS": ok, "POWER": [{"Watts": 3010, "PSU": False}]},
        temps={"STATUS": ok, "TEMPS": [{"ID": 0, "TEMP": 0, "TopLeft": 60, "BottomRight": 71},
                                       {"ID": 1, "TEMP": 1, "TopLeft": 55, "BottomRight": 69}]},
        fans={"STATUS": ok, "FANS": [{"ID": 0, "RPM": 4200, "Speed": 70}, {"ID": 1, "RPM": 4100, "Speed": 70}]},
    )
    assert r.online
    assert r.hashrate_ths == 141.0
    assert r.hashrate_avg_ths == 140.0
    assert r.uptime_s == 3600
    assert r.power_w == 3010 and r.power_measured is False
    assert r.temp_max_c == 71           # ignora ID/TEMP
    assert r.fan_rpm == [4200, 4100] and r.fan_pct == 70
    assert r.pool_user == "bc1qabc.s19"  # pega a pool com Stratum Active
    assert r.pool_alive is True
    assert r.warnings == []


def test_luxos_parse_warnings():
    ok = [{"STATUS": "S"}]
    r = luxos.parse(
        summary={"STATUS": ok, "SUMMARY": [{"GHS 5s": 10.0, "Device Hardware%": 12.5}]},
        pools={"STATUS": ok, "POOLS": [{"URL": "u", "User": "a.b", "Status": "Dead"}]},
        fans={"STATUS": ok, "FANS": [{"RPM": 0, "Speed": 100}]},
    )
    joined = " | ".join(r.warnings)
    assert "hardware" in joined and "ventoinha parada" in joined and "pool Dead" in joined


# ------------------------------------------------------------------ AxeOS
def test_axeos_parse_numeric_bestdiff():
    r = axeos.parse({
        "hashRate": 1150.0, "hashRate_1h": 1100.0, "expectedHashrate": 1200.0, "temp": 61.5, "vrTemp": 70,
        "power": 18.2, "fanrpm": 4000, "fan2rpm": 0, "fanspeed": 55, "uptimeSeconds": 99,
        "sharesAccepted": 10, "sharesRejected": 1, "bestDiff": 4.29e9,
        "stratumURL": "pool.defisurfers.xyz", "stratumPort": 2018, "stratumUser": "bc1q.bitaxe",
        "ASICModel": "BM1370", "axeOSVersion": "v2.15.4"})
    assert round(r.hashrate_ths, 3) == 1.15
    assert r.hashrate_avg_ths == 1.1 and r.expected_ths == 1.2
    assert r.temp_max_c == 61.5 and r.temp_vr_c == 70
    assert r.fan_rpm == [4000]          # fan2 = 0 é ignorada (não existe)
    assert r.best_diff == 4.29e9
    assert r.pool_url == "pool.defisurfers.xyz:2018"
    assert r.firmware == "v2.15.4"
    assert r.warnings == []


def test_axeos_parse_legacy_string_bestdiff_and_overheat():
    r = axeos.parse({"hashRate": 0, "bestDiff": "4.29G", "overheat_mode": 1, "fanrpm": 0, "fanspeed": 100})
    assert r.best_diff == 4.29e9
    assert any("SUPERAQUECIMENTO" in w for w in r.warnings)
    assert any("ventoinha parada" in w for w in r.warnings)


# ------------------------------------------------------------------ defisurfers
def test_defisurfers_parse_aggregates_sessions_and_online():
    client = {"bestDifficulty": 29973516261, "totalHashrate": 6.157e12, "workers": [
        {"name": "s19", "hashRate": 5e12, "bestDifficulty": "1920102528.00", "lastSeen": "2026-11-15T00:00:00.000Z"},
        {"name": "s19", "hashRate": 1e12, "bestDifficulty": "3000000000.00", "lastSeen": "2026-11-15T00:01:00.000Z"},
        {"name": "bitaxe", "hashRate": 1.1e12, "bestDifficulty": "5.00", "lastSeen": "2026-11-14T00:00:00.000Z"},
    ]}
    now = 1_794_700_920.0  # 2026-11-15T00:02:00Z
    r = defisurfers.parse(client,
                          chart=[{"label": "a", "data": 1e12}, {"label": "b", "data": 2e12}, {"label": "c", "data": 4e12}],
                          pplns={"balanceSats": 0, "totalPaidSats": 0, "currentWindowPercent": 3.2277},
                          pool={"totalHashRate": 9.67e17, "blocksFound": []}, now=now)
    s19 = next(w for w in r.workers if w.name == "s19")
    bitaxe = next(w for w in r.workers if w.name == "bitaxe")
    assert s19.hashrate_ths == 6.0 and s19.best_diff == 3e9 and s19.online
    assert not bitaxe.online
    assert r.hashrate_real_ths == 3.0   # média dos 2 últimos pontos
    assert r.pplns_percent == 3.2277 and r.balance_sats == 0
    assert r.blocks_found == 0 and round(r.pool_hashrate_ths) == 967000


# ------------------------------------------------------------------ Parasite
def test_parasite_parse_mixed_types():
    r = parasite.parse({
        "hashrate": 3283000000000000, "workers": 2, "lastSubmission": "38s ago", "bestDifficulty": "400.96T",
        "workerData": [
            {"id": "bc1q.nerd", "name": "nerd", "hashrate": "52000", "bestDifficulty": "1203344",
             "lastSubmission": str(int(NOW - 30))},
            {"id": "bc1q.old", "name": "old", "hashrate": "0", "bestDifficulty": "5",
             "lastSubmission": str(int(NOW - 90 * 86400))},
        ]}, now=NOW)
    assert r.best_diff == 400.96e12
    nerd = next(w for w in r.workers if w.name == "nerd")
    old = next(w for w in r.workers if w.name == "old")
    assert nerd.online and nerd.hashrate_ths == 52000 / 1e12
    assert not old.online
