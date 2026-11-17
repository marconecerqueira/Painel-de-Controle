# Alertas no Telegram: configuração (5 minutos)

## 1. Criar o bot

1. No Telegram, abra uma conversa com **@BotFather**. É o bot oficial, com selo azul de verificado.
2. Envie `/newbot` e escolha um nome (ex.: *Painel de Controle*) e um usuário terminado em `bot` (ex.: `minerador_mc_bot`).
3. O BotFather responde com um **token**, parecido com `8123456789:AAH...xyz`.
   **Trate esse token como senha:** quem tiver o token controla o bot.

## 2. Descobrir o seu chat_id

1. Abra o seu bot novo no Telegram e envie qualquer mensagem (ex.: `/start`).
2. No terminal, dentro da pasta do projeto, rode:
   ```bash
   .venv/bin/python -m minerador --telegram-chatid SEU_TOKEN
   ```
   Ele mostra algo como `chat_id = "123456789"   (Marcone)`.

> Quer os alertas num **grupo** (por exemplo, com um sócio)? Adicione o bot ao grupo, mande uma mensagem lá e rode o comando de novo. O `chat_id` do grupo começa com `-`.

## 3. Configurar e testar

No `config.toml`:

```toml
[telegram]
bot_token = "8123456789:AAH...xyz"
chat_id = "123456789"
```

Teste:

```bash
.venv/bin/python -m minerador --config config.toml --telegram-test
```

Deve chegar a mensagem "✅ Painel de Controle: alertas do Telegram configurados com sucesso". Depois, reinicie o painel (no Pi: `sudo systemctl restart minerador`).

## Quais alertas existem

| Alerta | Dispara depois de | Mensagem de volta |
|---|---|---|
| 🔴 Máquina offline | 3 min | ✅ voltou a ficar online |
| 🔴 Hashrate abaixo de 70% do esperado | 10 min | ✅ depois de 5 min normal |
| 🔴 Temperatura do chip ou do VR acima do limite | 1 min | ✅ |
| 🔴 Bitaxe em modo de superaquecimento | na hora | ✅ |
| 🔴 Ventoinha parada | 1 min | ✅ |
| 🔴 Máquina ligada, mas a pool sem receber shares | 3 min | ✅ |
| 🔴 Pool fora do ar | 10 min | ✅ |
| ℹ️ Aluguel MRR ativo / ⏹ terminou | 3 min | — |

* **Problemas rápidos não geram alerta.** Se o problema some antes do tempo de espera, nenhuma mensagem é enviada.
* **Reiniciar o Pi não reenvia alertas.** O estado fica salvo no banco.
* **Sem Telegram, os alertas continuam visíveis.** Eles aparecem na seção **Alertas** do painel.

Para mudar os tempos, adicione ao `config.toml`:

```toml
[alerts]
offline_min = 3
hashrate_min = 10
temp_min = 1
pool_min = 10
rental_min = 3
low_hashrate_pct = 70     # % do hashrate esperado (expected_ths de cada máquina)
```

Os limites de temperatura ficam em cada máquina: `temp_max_c` e `vr_temp_max_c`.
