"""Servidor FastAPI: API JSON + painel estático."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .alerts import AlertEngine, TelegramNotifier
from .blocks import BlockFeed
from .collector import Collector
from .control import ControlError
from .config import Config
from .db import Database

STATIC_DIR = Path(__file__).parent / "static"


class AckBody(BaseModel):
    key: str | None = None      # None = todos os alertas ativos
    undo: bool = False          # True = volta a mostrar no aviso


class ControlBody(BaseModel):
    action: str
    value: str | dict | None = None   # perfil (LuxOS) ou {frequency, coreVoltage} (AxeOS)


class EventBody(BaseModel):
    ts: float
    key: str
    event: str


def create_app(cfg: Config, start_collector: bool = True) -> FastAPI:
    db = Database(cfg.db_path)
    notifier = (TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
                if cfg.telegram_token else None)
    collector = Collector(cfg, db, AlertEngine(cfg.alerts, db, notifier))
    blocks = BlockFeed(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(collector.run()) if start_collector else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await blocks.aclose()
            db.close()

    app = FastAPI(title="Painel de Controle", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.collector = collector

    # O painel não manda prazo de cache: sem isto o Safari mostrava o logo/ícones antigos.
    # "no-cache" = pode guardar, mas confere com o servidor antes de usar (resposta 304 é barata).
    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/status")
    async def status() -> dict:
        return collector.snapshot()

    @app.get("/api/history")
    async def history(range: str = "24h") -> dict:
        try:
            return await collector.history(range)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/blocks")
    async def found_blocks(view: str = "rede") -> dict:
        try:
            return await blocks.get(view)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/blocks/pools")
    async def block_pools() -> dict:
        return await blocks.pool_list()

    # ------------------------------------------------------------------ ações do painel
    # Exigem o cabeçalho X-Minerador: um site qualquer aberto no navegador não consegue
    # mandá-lo para cá sem permissão (CORS), então não dá para fechar cards/alertas "por fora".
    def _guard(request: Request) -> None:
        if request.headers.get("x-minerador") != "1":
            raise HTTPException(status_code=403, detail="cabeçalho X-Minerador ausente")

    @app.post("/api/miners/{miner_id}/hide")
    async def hide_miner(miner_id: str, request: Request) -> dict:
        _guard(request)
        try:
            await collector.set_hidden(miner_id, True)
        except KeyError:
            raise HTTPException(status_code=404, detail="máquina não encontrada") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True}

    @app.post("/api/miners/{miner_id}/show")
    async def show_miner(miner_id: str, request: Request) -> dict:
        _guard(request)
        try:
            await collector.set_hidden(miner_id, False)
        except KeyError:
            raise HTTPException(status_code=404, detail="máquina não encontrada") from None
        return {"ok": True}

    # ------------------------------------------------------------------ controle remoto (Fase 4)
    @app.get("/api/miners/{miner_id}/control")
    async def control_options(miner_id: str) -> dict:
        try:
            return await collector.control_options(miner_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="máquina não encontrada") from None
        except ValueError as e:        # sem controle (só pela pool)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ControlError as e:      # máquina não respondeu
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.post("/api/miners/{miner_id}/control")
    async def control_run(miner_id: str, body: ControlBody, request: Request) -> dict:
        _guard(request)
        try:
            msg = await collector.control(miner_id, body.action, body.value)
        except KeyError:
            raise HTTPException(status_code=404, detail="máquina não encontrada") from None
        except ControlError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True, "message": msg}

    def _engine() -> AlertEngine:
        if collector.alerts is None:
            raise HTTPException(status_code=404, detail="alertas desativados")
        return collector.alerts

    @app.post("/api/alerts/ack")
    async def ack_alert(body: AckBody, request: Request) -> dict:
        _guard(request)
        return {"changed": await _engine().ack(body.key, not body.undo)}

    @app.post("/api/alerts/events/delete")
    async def delete_event(body: EventBody, request: Request) -> dict:
        _guard(request)
        return {"deleted": await _engine().delete_event(body.ts, body.key, body.event)}

    @app.post("/api/alerts/events/clear")
    async def clear_events(request: Request) -> dict:
        _guard(request)
        await _engine().clear_events()
        return {"ok": True}

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # O Safari procura esses caminhos na raiz; sem eles o app no Dock vira um monograma com letra.
    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    async def apple_touch_icon() -> FileResponse:
        return FileResponse(STATIC_DIR / "apple-touch-icon.png")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon-32.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
