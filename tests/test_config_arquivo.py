"""Leitura do config.toml do disco (BOM do Bloco de Notas, erro de digitação)."""

import shutil
from pathlib import Path

import pytest

from minerador.config import ConfigError, load_config

EXEMPLO = Path(__file__).resolve().parent.parent / "config.example.toml"


def test_aceita_bom_do_bloco_de_notas(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(b"\xef\xbb\xbf" + EXEMPLO.read_bytes())
    assert load_config(cfg).port == 8080


def test_erro_de_digitacao_vira_mensagem(tmp_path):
    cfg = tmp_path / "config.toml"
    shutil.copy(EXEMPLO, cfg)
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nport = = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="erro de digitação"):
        load_config(cfg)
