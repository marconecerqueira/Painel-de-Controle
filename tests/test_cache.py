"""O navegador deve sempre conferir se o painel e os ícones mudaram (logo novo não aparecia)."""

from fastapi.testclient import TestClient

from minerador.app import create_app
from minerador.config import parse_config


def test_static_assets_are_revalidated(tmp_path):
    cfg = parse_config({"server": {"db_path": str(tmp_path / "t.db")}})
    with TestClient(create_app(cfg, start_collector=False)) as c:
        for path in ("/", "/static/icon.svg", "/apple-touch-icon.png",
                     "/favicon.ico", "/static/manifest.webmanifest"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.headers.get("cache-control") == "no-cache", path
        html = c.get("/").text
        for asset in ("icon.svg", "favicon-32.png", "apple-touch-icon.png", "manifest.webmanifest"):
            assert f"/static/{asset}?v=" in html, asset
        assert "?v=" in c.get("/static/manifest.webmanifest").text
        assert c.get("/api/health").headers.get("cache-control") is None   # a API não precisa
