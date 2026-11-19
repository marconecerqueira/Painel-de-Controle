"""[location]: local fixo do cartão de hora e tempo."""

import pytest
from fastapi.testclient import TestClient

from minerador.app import create_app
from minerador.config import ConfigError, parse_config


def test_sem_location_usa_o_aparelho():
    assert parse_config({}).location is None


def test_location_completa():
    cfg = parse_config({"location": {"name": " Porto ", "country": "Portugal", "country_code": "pt",
                                     "lat": 41.1496, "lon": -8.611}})
    assert cfg.location == {"name": "Porto", "country": "Portugal", "country_code": "PT",
                            "lat": 41.1496, "lon": -8.611}


@pytest.mark.parametrize("loc", [
    {"name": "Porto", "lat": 41.1},                          # sem lon
    {"name": "Porto", "lat": "norte", "lon": -8.6},          # lat não numérica
    {"lat": 41.1, "lon": -8.6},                              # sem name
    {"name": "Porto", "lat": 141.1, "lon": -8.6},            # fora da faixa
    {"name": "Porto", "lat": 41.1, "lon": -8.6, "country_code": "POR"},
])
def test_location_invalida(loc):
    with pytest.raises(ConfigError):
        parse_config({"location": loc})


def test_api_location(tmp_path):
    cfg = parse_config({"server": {"db_path": str(tmp_path / "t.db")},
                        "location": {"name": "Porto", "country_code": "PT", "lat": 41.1496, "lon": -8.611}})
    with TestClient(create_app(cfg, start_collector=False)) as client:
        assert client.get("/api/location").json()["location"]["name"] == "Porto"
