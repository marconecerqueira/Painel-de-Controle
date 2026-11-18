"""Controle remoto das máquinas (Fase 4).

LuxOS (S19j XP), API TCP 4028 — docs: https://docs.luxor.tech/firmware/api/intro
  - comandos que mudam algo exigem sessão: logon -> SessionID como 1º parâmetro -> logoff;
  - só existe UMA sessão por vez (expira em 1 min). Se outra estiver aberta (ex.: a interface
    web do LuxOS), o painel avisa e NÃO derruba a sessão alheia (não usa "kill");
  - rebootdevice / resetminer derrubam a sessão sozinhos;
  - curtail sleep: a máquina só volta com wakeup (vale também depois de reiniciar).

AxeOS (Bitaxe), HTTP:
  - POST /api/system/restart;
  - PATCH /api/system {frequency, coreVoltage}: o firmware NÃO valida faixas seguras, então
    só aceitamos os valores listados pelo próprio firmware em GET /api/system/asic.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import httpx

from .models import describe_error, parse_number
from .sources import luxos

LUXOS_ACTIONS = ("profile", "sleep", "wakeup", "restart", "reboot")
AXEOS_ACTIONS = ("tune", "restart")

LABELS = {
    "profile": "trocar o perfil de potência",
    "sleep": "dormir (parar de minerar)",
    "wakeup": "acordar (voltar a minerar)",
    "restart": "reiniciar a mineração",
    "reboot": "reiniciar a máquina",
    "tune": "ajustar frequência e voltagem",
}


class ControlError(RuntimeError):
    """Erro com mensagem pronta para mostrar no painel."""


# ------------------------------------------------------------------ LuxOS
async def _lux(host: str, port: int, cmd: str, param: str | None = None) -> dict:
    try:
        return await luxos.command(host, port, cmd, param, timeout=8.0)
    except (OSError, asyncio.TimeoutError) as e:
        raise ControlError(f"sem resposta da S19 (LuxOS): {describe_error(e)}") from e


def _rows(resp: dict, key: str) -> list[dict]:
    return [x for x in (resp.get(key) or []) if isinstance(x, dict)]


async def luxos_options(host: str, port: int) -> dict:
    try:
        profiles = await _lux(host, port, "profiles")
        config = await _lux(host, port, "config")
    except luxos.LuxOSError as e:
        raise ControlError(f"o LuxOS recusou a consulta: {e}") from e
    cfg = (_rows(config, "CONFIG") or [{}])[0]
    out = []
    for p in _rows(profiles, "PROFILES"):
        name = p.get("Profile Name")
        if not name:
            continue
        try:                          # Step vem como texto e pode ser negativo ("-14")
            step = int(float(p.get("Step")))
        except (TypeError, ValueError):
            step = None
        out.append({"name": name, "watts": parse_number(p.get("Watts")),
                    "ths": parse_number(p.get("Hashrate")), "step": step})
    out.sort(key=lambda p: (p["step"] is None, p["step"] or 0))
    return {"type": "luxos", "actions": list(LUXOS_ACTIONS), "profiles": out,
            "current_profile": cfg.get("Profile"),
            "sleeping": str(cfg.get("CurtailMode") or "").lower() == "sleep"}


async def luxos_run(host: str, port: int, action: str, value=None) -> None:
    if action not in LUXOS_ACTIONS:
        raise ControlError(f"ação desconhecida: {action}")
    if action == "profile":
        names = {p["name"] for p in (await luxos_options(host, port))["profiles"]}
        if not isinstance(value, str) or "," in value or value not in names:
            raise ControlError(f"perfil inválido: {value!r} não está na lista da máquina")

    try:
        resp = await _lux(host, port, "logon")
    except luxos.LuxOSError:
        raise ControlError("o LuxOS já tem outra sessão aberta (a interface web do LuxOS está "
                           "aberta?). Feche-a ou espere 1 minuto e tente de novo.") from None
    sid = ((_rows(resp, "SESSION") or [{}])[0]).get("SessionID")
    if not sid:
        raise ControlError("o LuxOS não abriu a sessão; tente de novo em 1 minuto")

    cmd, param = {
        "profile": ("profileset", f"{sid},{value}"),
        "sleep": ("curtail", f"{sid},sleep"),
        "wakeup": ("curtail", f"{sid},wakeup"),
        "restart": ("resetminer", sid),
        "reboot": ("rebootdevice", sid),
    }[action]
    try:
        await _lux(host, port, cmd, param)
    except luxos.LuxOSError as e:
        raise ControlError(f"o LuxOS recusou o comando: {e}") from e
    finally:
        if action not in ("restart", "reboot"):      # reiniciar já derruba a sessão
            with suppress(ControlError, luxos.LuxOSError):
                await _lux(host, port, "logoff", sid)


# ------------------------------------------------------------------ AxeOS
def _base(host: str, port: int | None) -> str:
    return f"http://{host}" if port in (None, 80) else f"http://{host}:{port}"


async def _axe(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    try:
        resp = await client.request(method, url, timeout=10, **kw)
        resp.raise_for_status()
        return resp
    except httpx.HTTPError as e:
        raise ControlError(f"sem resposta da Bitaxe (AxeOS): {describe_error(e)}") from e


async def axeos_options(client: httpx.AsyncClient, host: str, port: int | None) -> dict:
    base = _base(host, port)
    try:
        asic = (await _axe(client, "GET", f"{base}/api/system/asic")).json()
        info = (await _axe(client, "GET", f"{base}/api/system/info")).json()
    except ValueError as e:
        raise ControlError("a Bitaxe respondeu num formato inesperado") from e
    nums = lambda xs: [int(x) for x in (xs or []) if isinstance(x, (int, float)) and not isinstance(x, bool)]  # noqa: E731
    return {"type": "axeos", "actions": list(AXEOS_ACTIONS),
            "frequency_options": nums(asic.get("frequencyOptions")),
            "voltage_options": nums(asic.get("voltageOptions")),
            "default_frequency": parse_number(asic.get("defaultFrequency")),
            "default_voltage": parse_number(asic.get("defaultVoltage")),
            "frequency": parse_number(info.get("frequency")),
            "core_voltage": parse_number(info.get("coreVoltage"))}


async def axeos_run(client: httpx.AsyncClient, host: str, port: int | None, action: str, value=None) -> None:
    base = _base(host, port)
    if action == "restart":
        await _axe(client, "POST", f"{base}/api/system/restart")
        return
    if action != "tune":
        raise ControlError(f"ação desconhecida: {action}")
    opts = await axeos_options(client, host, port)
    v = value if isinstance(value, dict) else {}
    freq, volt = v.get("frequency"), v.get("coreVoltage")
    if freq not in opts["frequency_options"]:
        raise ControlError(f"frequência {freq!r} MHz fora da lista segura da Bitaxe")
    if volt not in opts["voltage_options"]:
        raise ControlError(f"voltagem {volt!r} mV fora da lista segura da Bitaxe")
    await _axe(client, "PATCH", f"{base}/api/system", json={"frequency": freq, "coreVoltage": volt})
