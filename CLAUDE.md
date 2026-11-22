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
.venv/bin/python -m minerador --procurar [192.168.1.0/24]     # acha AxeOS/LuxOS na rede local (só lê) e imprime o [[miners]]
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
- **`alerts.py`**: `conditions(snapshot)` → `AlertEngine`. O engine aplica tempos de espera (`[alerts]`) e folga na temperatura (alerta de temp./VR que já disparou só some `temp_hyst_c` = 5 °C abaixo do limite), guarda o histórico no banco e envia pelo Telegram. No painel **não há lista/histórico de alertas** (removida a pedido do usuário): os ativos (`alert` 🔴 e `info` ℹ️) aparecem só no aviso do topo, cada um com × que chama `/api/alerts/ack`.
- **`finance.py`**: hashprice, dificuldade e preço vêm de mempool.space (`Market`). Calcula:
  - `compute_own`: receita esperada × energia.
  - `compute_rental`: custo do aluguel × valor esperado / chance de bloco solo; `result_eur` = valor esperado − custo.
  - Aluguel que termina vai para a tabela `rental_results` (gravado **uma vez só**, valores congelados no fim). Isso acontece no estado "terminado" **e também** quando aparece um aluguel novo ("novo"), porque aluguéis encostados pulam o "terminado". Assim ele sobrevive ao × do cartão e à troca do config. Uma linha com `"ignored": true` deixa um aluguel fora da lista de propósito (foi o caso do aluguel solo de 16/11, a pedido do usuário). O snapshot manda `rentals_done` (lista "Aluguéis terminados") e `totals.rentals` (terminados + ativos; `missing` = aluguéis "novo" sem custo).
  - Avisos de finanças: `note` em português simples + `note_hint` com a chave do config. Na tela aparecem como `.todo` (pendência), não `.err`.
  - Na tela, as finanças abrem com uma **frase de veredito** (`financeVerdict`, `rentalVerdict`): ganha/perde quanto e por quê (tarifa × ponto de empate), logo abaixo do cabeçalho do cartão. Máquina offline mostra "Agora" = só a energia (`nowProfit`), com a média de 24 h ao lado. O topo começa pelo dinheiro (`moneyTop`): "<Mês> até hoje (estimativa)" em destaque (média de 24 h × dias do mês + aluguéis do mês) e a linha viva `nowLine` ("Agora perdendo X € por dia — <máquina> parada"). Hashrate, máquinas ativas, potência e eficiência ficam menores, embaixo (`.stats`). O total de sempre dos aluguéis fica só na lista "Aluguéis terminados". Cada gaveta do rack mostra o €/dia. Os valores usam o formato pt-PT ("0,30 €"). Os detalhes do aluguel ficam num `<details data-keep>`, que continua aberto quando a lista é redesenhada.
- **`control.py`** (Fase 4) envia comandos às máquinas. No LuxOS: `logon` → comando → `logoff`, com **uma sessão por vez**. No AxeOS: só valores seguros vindos de `/api/system/asic`. Há um lock por máquina no `Collector`. Nunca foi testado contra hardware real, só no simulador.
- **`blocks.py`** (`BlockFeed`) alimenta a seção "Blocos encontrados" (filtros `rede`, `minhas`, `solo`, `pool:<slug>`). Ela junta mempool.space, os blocos da defisurfers e os da Hash Commander, com cache por TTL. Os blocos pendentes (fila do mempool) só vêm na visão `rede`.

Todos os endpoints `POST` exigem o cabeçalho `X-Minerador: 1`, que funciona como proteção CSRF via CORS. O front-end e o app de barra de menu já o enviam.

**Front-end:** `minerador/static/index.html` é um arquivo único (HTML + CSS + JS puro, sem framework, sem build). As cores são tokens CSS em `:root`, redefinidos em `prefers-color-scheme: dark`; as fontes Barlow são servidas localmente de `static/fonts/`. O painel faz polling de `/api/status`, `/api/history` e `/api/blocks` com `setInterval`. O gráfico de hashrate é de **barras** (pedido do usuário em 22/11, depois de experimentar linhas e velas): número grande = total ao vivo (`totals.hashrate_ths`, o mesmo do topo) com % vs média do período, ~50 barras com a soma das máquinas visíveis (o total da pool fica de fora, já está dentro das máquinas), a mais recente em laranja, e 4 quadrinhos (média, pico, mais baixo, última leitura). Cada máquina aparece só no balão do mouse e na tabela. O topo (rack) e os cartões das máquinas usam o mesmo fundo dos outros cartões (sem a placa verde). O logo fica no cartão do topo, no tamanho de sempre (104 px; 84 no celular), e é animado (`.brand-stage`): arco de luz verde girando (`.brand-arc`, só `transform`) e faíscas piscando (`.bs`, criadas no JS, nenhuma embaixo, onde fica o "Ao vivo"). O usuário **não quer** um cartão separado para o logo. Tudo para no modo leve, com `prefers-reduced-motion` e fora da tela (classe `paused` via IntersectionObserver). Cartão de máquina (`.mcard`): linha curta (tipo · modelo · firmware), caixa "Ligada há" (ou "Último share" para quem é só pela pool), gráfico pequeno do hashrate (`sparkSvg`, 40 médias do período do gráfico de cima, cor fixa por máquina), a faixa discreta das últimas 24 h (`dayStrip`, uma casa por hora do relógio: normal / fraca < 70% / parada, sempre de `/api/history?range=24h` em `hist24`) e os números em lista, com o "Recorde de share" e a data (`best_share`, tabela `best_shares`: maior entre a máquina e a pool; na 1ª vez sai das leituras guardadas, para a data ser a real; não entra na limpeza); o Financeiro vem depois. Os títulos de seção (`h2`) são pequenos, em maiúsculas espaçadas. "Leitura há…" (`stampOf`) e a caixa "Último share" ficam amarelos (`stale`) com mais de 5 min sem dado novo (aluguel `idle` não). As grades de cartões (`.grid`) usam flex-wrap com `flex: 1 1 330px` e o rack usa `auto-fit`, para não sobrar coluna vazia nem buraco entre cartões de alturas diferentes. Os blocos são desenhados como cubos 3D neon em CSS: os que estão na fila ficam à esquerda e os minerados à direita. A variável `--t` mantém a fase das animações quando a lista é redesenhada. Logos das pools vêm de `{mempool}/resources/mining-pools/{slug}.svg`. Exceções: defisurfers usa ⛏️; a Hash Commander é carregada do site dela e **não** é copiada para o repo, porque o arquivo proíbe redistribuição. A seta `.neo-center` volta a fileira para a divisória fila | minerados.

**Festa:** `festaCheck` dispara fogos (canvas) para blocos `found_by_me`/`paid_me` das últimas 6 h ainda não vistos neste aparelho (localStorage `minerador.festa.vistos`). `festaPoll` consulta `view=minhas` a cada 3 min. Demonstração com `?festa=1`.

Exceção a "tudo passa pelo backend": a seção **Mempool** (só taxa mínima, memória e não confirmadas; o Goggles e o gráfico de transações recebidas foram removidos a pedido do usuário) liga o **navegador direto** ao mempool.space pelo websocket `/api/v1/ws` com `want: ["stats"]` (só o `mempoolInfo`).

O Python não tem cliente websocket. A base vem de `mempool_url`.

O cartão **hora local e tempo**, por baixo do gráfico de hashrate, também roda só no navegador:
- se o `config.toml` tiver `[location]` (exposto em `/api/location`), usa esse local fixo, e a hora mostrada passa a ser a do fuso dele (vem da Open-Meteo). O hosting no Porto está configurado assim;
- senão, localização pelo GPS do aparelho; se a permissão for negada, aproximada pelo IP via BigDataCloud `reverse-geocode-client`;
- tempo pela Open-Meteo, com códigos WMO → ícone SVG animado.

Ambas as APIs são sem chave.

Visual do cartão:
- **Fundo:** a classe `sky-night|sunny|cloudy|rain|snow` vem do código WMO e de `is_day`. As camadas do céu animam só com `transform` (sem `background-position`), para não forçar repintura.
- **Globo:** WebGL num canvas fora da tela, com as texturas `static/img/terra-*.jpg`, composto num canvas 2D. A ordem de desenho é metade de trás do anel → globo → metade da frente. O sol percorre o anel e serve de direção da luz; o ponto verde marca o local.
- **Modo leve:** interruptor no topo, guardado em localStorage `minerador.leve`. Põe a classe `html.leve`, que zera as animações CSS de `.neo`/`.wx` e tira os brilhos externos. Nesse modo o globo desenha um quadro só (`globeKick` redesenha quando algo muda). Toda animação nova deve respeitar `leve`.
- **Desempenho:** a máquina do usuário fica sem memória com facilidade. Mantenha as animações baratas e parar quando estiverem fora da tela. O middleware em `app.py` força `Cache-Control: no-cache` em tudo que não é `/api/`, porque o Safari mostrava ícones antigos.

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
