# Painel de Controle

Painel de mineradores (pacote Python `minerador`).

Painel para ver e controlar, de qualquer lugar, mineradores de Bitcoin que ficam num hosting de terceiros. O app roda num Raspberry Pi na rede das máquinas e é acessado pela Tailscale. Nada fica exposto na internet.

| Máquina | Como é lida |
|---|---|
| Antminer S19j XP (LuxOS) | API TCP 4028 |
| Bitaxe (AxeOS) | API HTTP |
| NerdMiner | pela pool (Parasite) |
| pool.defisurfers.xyz / Parasite | APIs públicas das pools |
| Hash Commander (Bitcoin Cash, PPLNS + solo) | API pública da pool |
| Blocos encontrados (rede, suas pools, solo, qualquer pool) | mempool.space |

* Design completo: [docs/especificacao.md](docs/especificacao.md)
* Instalação: [docs/instalacao-pi.md](docs/instalacao-pi.md)

## Testar no Mac sem hardware (simulador)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python tools/simulador.py &                         # finge ser S19, Bitaxe e pools
.venv/bin/python -m minerador --config config.demo.toml      # abre em http://localhost:8080
```

## Testes

```bash
.venv/bin/pytest
```

## App de barra de menu (Mac)

Mostra o hashrate total ao lado do relógio; ao clicar, os totais, os alertas ativos e as máquinas,
com o controle remoto (perfil, dormir/acordar, reiniciar) quando o painel fala direto com a máquina.

```bash
macos/build.sh        # compila (só precisa das Command Line Tools), instala em ~/Applications e abre
```

Nos ajustes do app (ícone de engrenagem): endereço do painel (padrão `http://127.0.0.1:8080`; com o
painel no Pi, use o endereço da Tailscale) e "Abrir ao iniciar o Mac".

## Estrutura

```
minerador/
  sources/      um coletor por fonte (luxos, axeos, defisurfers, parasite)
  collector.py  laço de coleta + regras de status
  db.py         SQLite (leituras brutas, limpeza automática)
  blocks.py     blocos encontrados (mempool.space + defisurfers) com filtros e cache
  app.py        FastAPI: /api/status, /api/history, /api/blocks e o painel
  static/       painel web (um único index.html)
tools/simulador.py   imita as APIs reais para teste
macos/               app de barra de menu (SwiftUI) + build.sh
deploy/              serviço systemd
```

## Roadmap

- [x] **Fase 1:** coletores e painel em tempo real
- [x] Fase 2a: gráfico de hashrate (24h · 3d · 7d · 30d)
- [x] Fase 2b: alertas no Telegram ([docs/alertas-telegram.md](docs/alertas-telegram.md))
- [x] Fase 2c: blocos encontrados via mempool.space, com filtros (Rede · Minhas pools · Solo e pequenas · qualquer pool)
- [x] Fase 3: financeiro
  - [x] aluguéis de hashrate: custo × valor esperado, chance de bloco no solo, preço pago × hashprice
  - [x] máquinas próprias: receita esperada (hashrate médio de 24 h) × custo de energia (`[finance] energy_eur_kwh`, `power_w`)
- [x] Fase 4: controle remoto e app de barra de menu no Mac
  - [x] controle remoto pelo painel (botão "Controle" no card): S19j XP/LuxOS — perfil de potência, dormir/acordar, reiniciar mineração/máquina; Bitaxe/AxeOS — frequência e voltagem (só valores seguros do firmware) e reiniciar. Confirmação antes de cada ação; cada comando entra no histórico e no Telegram. Testado no simulador; precisa do painel na rede das máquinas (Pi/PC)
  - [x] app de barra de menu (SwiftUI `MenuBarExtra`, `macos/build.sh`): hashrate na barra, totais, alertas, máquinas e controle; visual Matrix
