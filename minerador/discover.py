"""Procura mineradores na rede local: AxeOS (HTTP /api/system/info) e LuxOS (TCP 4028, "version").

Só lê; não muda nada nas máquinas. Uso: python -m minerador --procurar [192.168.1.0/24]
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket

import httpx

from .sources import luxos

MAX_HOSTS = 1024        # uma rede de casa/hosting é /24 (254 endereços); acima disso é engano


def local_network() -> ipaddress.IPv4Network:
    """A /24 do endereço deste computador (UDP "connect" não envia nada, só escolhe a interface)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def check_network(text: str | None) -> ipaddress.IPv4Network:
    net = local_network() if not text else ipaddress.ip_network(text, strict=False)
    if net.version != 4 or not net.is_private:
        raise ValueError(f"{net}: só redes locais (ex.: 192.168.1.0/24)")
    if net.num_addresses > MAX_HOSTS:
        raise ValueError(f"{net}: rede grande demais ({net.num_addresses} endereços); use uma /24")
    return net


def axeos_found(host: str, info) -> dict | None:
    """Resposta de /api/system/info -> máquina, ou None se não for um AxeOS."""
    if not isinstance(info, dict) or not ("ASICModel" in info or "hashRate" in info):
        return None
    model = " ".join(str(x) for x in (info.get("ASICModel"), info.get("boardVersion")) if x)
    return {"type": "axeos", "host": host, "name": str(info.get("hostname") or "Bitaxe"),
            "model": model or None, "firmware": info.get("axeOSVersion") or info.get("version")}


def luxos_found(host: str, version) -> dict | None:
    """Resposta do comando "version" na porta 4028 -> máquina, ou None se não for LuxOS."""
    v = ((version or {}).get("VERSION") or [{}])[0]
    if not v.get("LUXminer"):
        return None
    return {"type": "luxos", "host": host, "name": str(v.get("Type") or "Antminer"),
            "model": v.get("Type"), "firmware": v["LUXminer"]}


async def _probe(client: httpx.AsyncClient, host: str, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        try:
            r = await client.get(f"http://{host}/api/system/info", timeout=2.0)
            found = axeos_found(host, r.json()) if r.status_code == 200 else None
            if found:
                return found
        except (httpx.HTTPError, ValueError):
            pass
        try:
            return luxos_found(host, await luxos.command(host, 4028, "version", timeout=2.0))
        except (OSError, asyncio.TimeoutError, luxos.LuxOSError):
            return None


async def scan(net: ipaddress.IPv4Network) -> list[dict]:
    sem = asyncio.Semaphore(48)          # poucas conexões de cada vez: leve para o Pi e para a rede
    async with httpx.AsyncClient(headers={"User-Agent": "minerador/0.1"}) as client:
        found = await asyncio.gather(*(_probe(client, str(h), sem) for h in net.hosts()))
    return [f for f in found if f]


def config_snippet(m: dict, taken: set[str]) -> str:
    """Bloco [[miners]] pronto para colar no config.toml (com id que ainda não existe)."""
    base = re.sub(r"[^a-z0-9]+", "-", m["name"].lower()).strip("-") or m["type"]
    mid, n = base, 2
    while mid in taken:
        mid, n = f"{base}-{n}", n + 1
    taken.add(mid)
    return (f'[[miners]]\nid = "{mid}"\nname = "{m["name"]}"\ntype = "{m["type"]}"\n'
            f'host = "{m["host"]}"\n# pool = "..."   # tire o # e ponha o nome de [pools.*] onde ela minera\n')
