# Minerador — Painel remoto de mineradores

Especificação de design · 2026-11-15

## 1. Objetivo

Ver e controlar, de qualquer lugar, os mineradores de Bitcoin que ficam num hosting de terceiros, sem abrir portas no roteador do hosting e sem depender de serviço na nuvem.

## 2. Equipamentos e pools

| Máquina | Firmware / API local | Pool | Endereço stratum |
|---|---|---|---|
| Antminer S19j XP | LuxOS, TCP 4028 (cgminer‑compatível) | pool.defisurfers.xyz | `pool.defisurfers.xyz:2020` |
| Bitaxe | AxeOS, HTTP `/api/system/*` | pool.defisurfers.xyz | `pool.defisurfers.xyz:2018` |
| NerdMiner | sem API local útil | Parasite Pool | `parasite.wtf:42069` |

Tudo é SHA‑256 / BTC.

## 3. Arquitetura

```
┌──────────── REDE DO HOSTING ────────────┐
│ S19j XP (LuxOS :4028) ─┐                │
│ Bitaxe  (AxeOS HTTP) ──┼─▶ Raspberry Pi │
│ NerdMiner ┄┄┄┄┄┄┄┄┄┄┄┄┄┘   coletor      │
│                            SQLite       │
│                            FastAPI      │──▶ saída: APIs das pools,
│                            127.0.0.1    │    mempool.space, Telegram
└───────────────────────┬─────────────────┘
                        │ tailscale serve (HTTPS, só na tailnet)
                        ▼
        Mac: Safari → "Adicionar ao Dock" + app de barra de menu
```

* **Raspberry Pi** (Pi 4/5, preferencialmente com SSD USB) na mesma rede dos mineradores.
* **Coletor** em Python (asyncio) consulta cada fonte em intervalos fixos e guarda leituras no SQLite.
* **FastAPI** expõe `/api/*` (JSON) e o painel web estático.
* **Segurança:** o servidor escuta só em `127.0.0.1`. O acesso remoto é feito por `tailscale serve`, que publica o painel com HTTPS **apenas para os dispositivos da sua tailnet**. LuxOS e AxeOS não têm senha, então o painel nunca deve ficar exposto na LAN do hosting.

## 4. Fontes de dados

### 4.1 LuxOS (S19j XP): TCP 4028

Envia `{"command": "...", "parameter": "..."}`, recebe JSON e a conexão fecha a cada resposta. O sucesso é `STATUS[0].STATUS == "S"`.

| Comando | Campos usados |
|---|---|
| `summary` | `GHS 5s`, `GHS av` (GH/s), `Elapsed` (s), `Accepted`, `Rejected`, `Stale`, `Device Hardware%` |
| `power` | `POWER[0].Watts`, `PSU` (true = medido; false = estimado) |
| `temps` | por placa; chaves variam por modelo (`TopLeft`…), e o coletor pega o máximo numérico |
| `fans` | `RPM`, `Speed` (%) |
| `pools` | `URL`, `User`, `Status`, `Stratum Active` |

**Controle (Fase 4).** Os comandos exigem sessão: `logon` → `SessionID` como 1º parâmetro → `logoff`.

* Comandos: `rebootdevice`, `resetminer`, `profileset`, `curtail sleep|wakeup`.
* **Só existe uma sessão por vez** e ela expira em 1 min. Se a UI do LuxOS estiver aberta, o comando falha com "ocupado".

### 4.2 AxeOS (Bitaxe): HTTP

* `GET /api/system/info` retorna:
  * Hashrate em GH/s: `hashRate`, `hashRate_1h`, `expectedHashrate`.
  * Temperaturas em °C: `temp`, `vrTemp`.
  * Energia e ventoinha: `power` (W), `fanrpm`, `fanspeed`.
  * Shares e dificuldade: `sharesAccepted`, `sharesRejected`, `bestDiff`. `bestDiff` é string até a v2.10 e número a partir da v2.11.
  * Pool e estado: `uptimeSeconds`, `stratumURL`, `stratumUser`, `overheat_mode`, `version`/`axeOSVersion`.
* `GET /api/system/asic` traz as frequências e voltagens seguras (`frequencyOptions`, `voltageOptions`).
* **Controle (Fase 4):**
  * `POST /api/system/restart`.
  * `PATCH /api/system` com `{frequency, coreVoltage}`: aplica sem reiniciar, mas **o firmware não valida faixas seguras**. O painel só oferece valores de `/api/system/asic`.
* O AxeOS só aceita requisições de IPs privados. O Pi na LAN atende a esse requisito.

### 4.3 pool.defisurfers.xyz (fork de public-pool + PPLNS)

API pública, sem autenticação.

| Endpoint | Uso |
|---|---|
| `GET /api/client/{addr}` | `totalHashrate` (H/s), `workers[]{name, hashRate, bestDifficulty(string), lastSeen}` |
| `GET /api/client/{addr}/chart?range=1d` | série de 10 min. O hashrate "real" é a média dos 2 últimos pontos |
| `GET /api/pplns/{addr}` | `balanceSats`, `totalPaidSats`, `currentWindowPercent` |
| `GET /api/pool` | `totalHashRate`, `blocksFound` |

Cuidados:

* Endereço inválido devolve 200 com zeros.
* Workers sem conexão **somem** da lista. Por isso o coletor compara com os workers esperados.
* Taxa de 2%. Nenhum bloco encontrado até 2026‑11‑15.

### 4.4 Parasite Pool (parasite.space)

API pública e não documentada; o parser é tolerante.

| Endpoint | Uso |
|---|---|
| `GET /api/user/{addr}` | `hashrate` (número, H/s), `workerData[]{name, hashrate(string), bestDifficulty(string), lastSubmission(unix s, string)}`; 404 = não encontrado |
| `GET /api/user/{addr}/historical?period=24h&interval=5m` | histórico |

* Limite de cerca de 200 req/min. O painel consulta a cada 60 s.
* Ganhos são pagos via Lightning e não existe endpoint de saldo.

## 5. Regras de status

* **Online (local):** a API respondeu dentro do timeout.
* **Worker online (pool):** visto há menos de 10 min.
* **NerdMiner:** só existe o status da pool.
* **Alertas (Fase 2):**
  * Máquina offline há 3 min ou mais.
  * Hashrate abaixo de 70% do esperado por 10 min.
  * Chip acima de 80 °C na S19j XP.
  * Bitaxe com ASIC acima de 70 °C, VR acima de 90 °C ou `overheat_mode=1`.
  * Worker sumido da pool.

## 6. Financeiro (Fase 3)

* **Receita esperada por dia** = `hashrate_próprio / hashrate_rede × 144 × (subsídio + taxas médias)`.
  * Dados de rede vêm do mempool.space.
  * É o valor estatístico do hashrate, independente de sorte.
* **Recebido:** saldo e pagos da defisurfers. Para a Parasite, entrada manual ou leitura da carteira Lightning.
* **Custo de energia:** `W × 24 / 1000 × tarifa`. A tarifa é configurável (por kWh ou valor fixo por máquina por mês).
  * Config: `[finance] energy_eur_kwh`; por máquina `power_w` (nominal, vale quando a máquina não informa a potência) ou `energy_eur_month` (taxa fixa); por pool `fee_pct`.
  * Hashrate da receita: média das últimas 24 h gravada pelo painel (worker offline conta como 0).
  * Mostra também a tarifa de equilíbrio (€/kWh em que receita = energia) e quanto renderia no `expected_ths`.

## 7. Fases

| Fase | Entrega |
|---|---|
| **1** | Coletores (LuxOS, AxeOS, defisurfers, Parasite) + SQLite + painel web em tempo real + guia Pi/Tailscale |
| 2 | Gráficos de histórico + alertas no Telegram |
| 3 | Financeiro |
| 4 | Controle remoto pelo painel + app de barra de menu (SwiftUI `MenuBarExtra`) |

## 8. Fora de escopo

Rigs de GPU, hospedagem na nuvem e multiusuário.
