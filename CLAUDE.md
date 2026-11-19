# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Idioma

O usuário fala português. Responda em PT-BR, e mantenha textos da interface, logs, docstrings e mensagens de commit em português, como no resto do código. O nome visível do app é **"Painel de Controle"**; o pacote Python continua `minerador`.

## Comandos

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup (Python >= 3.11)
.venv/bin/pytest                                              # todos os testes
.venv/bin/pytest tests/test_blocos.py::nome_do_teste -q       # um teste só
.venv/bin/python tools/simulador.py &                         # finge ser LuxOS (:14028), AxeOS e pools (:18081)
.venv/bin/python -m minerador --config config.demo.toml       # painel em http://localhost:8080 contra o simulador
.venv/bin/python -m minerador --config config.toml --telegram-test
macos/build.sh                                                # compila/instala o app de barra de menu (SwiftUI)
```

Não há linter configurado nem etapa de build do front-end.

No Mac do usuário o painel real roda pelo **launchd** (`local.painel-de-controle`, log em `~/Library/Logs/painel-de-controle.log`). Depois de mudar código Python: `launchctl kickstart -k gui/$(id -u)/local.painel-de-controle`. Não use pkill + nohup, porque duplica o processo e a porta fica ocupada. Arquivos de `minerador/static/` são servidos direto do disco: basta recarregar a página.

## Arquitetura

Um único processo FastAPI (`app.create_app`) faz duas coisas: roda o laço de coleta como task asyncio no `lifespan` e serve a API JSON junto com o painel estático. Ele escuta só em `127.0.0.1`; o acesso remoto é por `tailscale serve`. LuxOS e AxeOS não têm senha, então o painel nunca deve ficar exposto na LAN.

Fluxo de dados:

- **`config.py`** lê `config.toml`, que não é commitado (ver `config.example.toml`). Cada entrada `[[miners]]` tem um `type`:
  - `luxos`: TCP 4028.
  - `axeos`: HTTP.
  - `pool_only`: status vem só da pool.
  - `rental`: hashrate alugado, com financeiro.
  
  Cada `[pools.*]` tem um `type`: `defisurfers`, `parasite` ou `hashcommander` (BCH). Máquinas são ligadas às pools por `pool` + `worker`; o worker é deduzido do usuário stratum `endereco.worker` quando omitido.
- **`sources/`**: um módulo por fonte. Cada um separa `parse(...)` (puro, testado com JSON fixo) de `fetch(...)` (I/O). Todos devolvem os modelos de `models.py` (`LocalReading`, `PoolReading`/`PoolWorker`). `parse_number` tolera números em string ("2,02 G" etc.).
- **`collector.py`** (`Collector`) consulta as máquinas a cada `intervals.local` e as pools a cada `intervals.pools`. Ele grava no SQLite (`db.py`) e monta o `snapshot()` servido em `/api/status`, onde o status de cada máquina combina a leitura local com o worker na pool. Detalhe importante: ele guarda a **última leitura boa** de cada pool (`pools_last_ok`, com tolerância `pool_grace_s`) para que uma falha passageira da API não gere alarme falso.
- **`alerts.py`**: `conditions(snapshot)` → `AlertEngine`. O engine aplica tempos de espera (`[alerts]`), guarda o histórico no banco e envia pelo Telegram.
- **`finance.py`**: hashprice, dificuldade e preço vêm de mempool.space (`Market`). Calcula:
  - `compute_own`: receita esperada × energia.
  - `compute_rental`: custo do aluguel × valor esperado / chance de bloco solo.
- **`control.py`** (Fase 4) envia comandos às máquinas. No LuxOS: `logon` → comando → `logoff`, com **uma sessão por vez**. No AxeOS: só valores seguros vindos de `/api/system/asic`. Há um lock por máquina no `Collector`. Nunca foi testado contra hardware real, só no simulador.
- **`blocks.py`** (`BlockFeed`) alimenta a seção "Blocos encontrados" (filtros `rede`, `minhas`, `solo`, `pool:<slug>`). Ela junta mempool.space, os blocos da defisurfers e os da Hash Commander, com cache por TTL. Os blocos pendentes (fila do mempool) só vêm na visão `rede`.

Todos os endpoints `POST` exigem o cabeçalho `X-Minerador: 1`, que funciona como proteção CSRF via CORS. O front-end e o app de barra de menu já o enviam.

**Front-end:** `minerador/static/index.html` é um arquivo único (HTML + CSS + JS puro, sem framework, sem build). As cores são tokens CSS em `:root`, redefinidos em `prefers-color-scheme: dark`; as fontes Barlow são servidas localmente de `static/fonts/`. O painel faz polling de `/api/status`, `/api/history` e `/api/blocks` com `setInterval`. Os blocos são desenhados como cubos 3D neon em CSS: os que estão na fila ficam à esquerda e os minerados à direita. A variável `--t` mantém a fase das animações quando a lista é redesenhada. Exceção a "tudo passa pelo backend": a seção **Mempool** (Goggles em canvas, taxa mínima, memória, não confirmadas e o gráfico de transações recebidas) liga o **navegador direto** ao mempool.space:
- websocket `/api/v1/ws` com `track-mempool-block: 0`, recebendo deltas;
- REST `/api/v1/statistics/2h`.

O Python não tem cliente websocket. A base vem de `mempool_url`, e os bits de flags seguem `TransactionFlags` do mempool.

O cartão **hora local e tempo**, por baixo do gráfico de hashrate, também roda só no navegador:
- localização pelo GPS do aparelho; se a permissão for negada, aproximada pelo IP via BigDataCloud `reverse-geocode-client`;
- tempo pela Open-Meteo, com códigos WMO → ícone SVG animado.

Ambas as APIs são sem chave. O middleware em `app.py` força `Cache-Control: no-cache` em tudo que não é `/api/`, porque o Safari mostrava ícones antigos.

**Mac:** `macos/PainelBarra.swift` é um app `MenuBarExtra` que consome a mesma API (padrão `http://127.0.0.1:8080`). Duas armadilhas conhecidas:
- Evite concatenar `String` com ternários encadeados em Swift: o compilador trava por muitos minutos.
- SF Symbols dentro do `Text` da barra não aparecem; use o caractere ₿.

## Testes

- Os testes de parser usam JSON fixo das APIs reais.
- `tests/test_integracao.py` roda o `Collector` de verdade contra o `tools/simulador.py`: LuxOS em TCP real numa porta efêmera, HTTP via transporte ASGI do httpx, sem rede.
- Ao adicionar uma fonte ou campo novo, atualize também o simulador.

## Restrições

- O repositório é **público** no GitHub. Nunca commite endereços de carteira reais, IPs, tokens ou o `config.toml`. Testes e o `config.demo.toml` usam endereços fictícios.
- `deploy/windows/`, `docs/instalacao-windows.md`, `.gitattributes`, `tests/test_config_arquivo.py` e as mudanças em `README.md`/`config.py` (tolerância a BOM) são trabalho do Windows **em espera e não commitado**. Não commite nem retome sem o usuário pedir.
- `deploy/minerador.service` e `docs/instalacao-pi.md` são específicos do Raspberry Pi (`User=pi`, `/home/pi/minerador`).
