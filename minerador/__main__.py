"""Uso:
  python -m minerador --config config.toml                   inicia o painel
  python -m minerador --telegram-chatid TOKEN                descobre o chat_id
  python -m minerador --config config.toml --telegram-test   envia mensagem de teste
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
import uvicorn

from .alerts import TelegramNotifier
from .app import create_app
from .config import ConfigError, load_config


def telegram_chat_ids(token: str) -> None:
    """Lista os chats que mandaram mensagem ao bot (getUpdates)."""
    api = f"https://api.telegram.org/bot{token}"

    def call(method: str) -> dict:
        try:
            return httpx.get(f"{api}/{method}", timeout=15).json()
        except (httpx.HTTPError, ValueError) as e:
            sys.exit(f"Não consegui falar com o Telegram: {str(e).replace(token, '<token>')}")

    data = call("getUpdates")
    if not data.get("ok") and data.get("error_code") == 409:
        # 409 = o bot está ligado a outro serviço por webhook (ex.: @ControllerBot, Manybot).
        print("Este bot estava conectado a outro serviço (webhook). Desconectando para uso do Painel de Controle…")
        if not call("deleteWebhook").get("ok"):
            sys.exit("Não consegui desconectar o webhook. Crie um bot novo no @BotFather.")
        print("Desconectado. Mande /start de novo para o seu bot e rode este comando outra vez.")
        return
    if not data.get("ok"):
        sys.exit(f"Telegram recusou o token: {data.get('description')}")
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username")
    if not chats:
        sys.exit("Nenhuma conversa encontrada. Abra o seu bot no Telegram, envie qualquer mensagem "
                 "(ex.: /start) e rode este comando de novo.")
    print("Conversas encontradas — copie o chat_id para o [telegram] do config.toml:")
    for cid, name in chats.items():
        print(f"  chat_id = \"{cid}\"   ({name})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="minerador", description="Painel remoto de mineradores")
    parser.add_argument("--config", "-c", default="config.toml", help="caminho do config.toml")
    parser.add_argument("--host", help="sobrescreve server.host")
    parser.add_argument("--port", type=int, help="sobrescreve server.port")
    parser.add_argument("--telegram-chatid", metavar="TOKEN", help="descobre o chat_id do seu bot")
    parser.add_argument("--telegram-test", action="store_true", help="envia uma mensagem de teste")
    args = parser.parse_args()

    if args.telegram_chatid:
        telegram_chat_ids(args.telegram_chatid)
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # o httpx loga a URL de cada requisição — a do Telegram contém o token do bot
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        sys.exit(f"Erro na configuração: {e}")

    if args.telegram_test:
        if not cfg.telegram_token:
            sys.exit("Preencha bot_token e chat_id em [telegram] no config.toml primeiro.")
        ok = asyncio.run(TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id).send(
            "✅ <b>Painel de Controle</b>\nAlertas do Telegram configurados com sucesso."))
        sys.exit(0 if ok else "Falhou — confira o token e o chat_id (detalhes no log acima).")

    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
