# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Um único usuário: o dono das máquinas. Ele fala português, mora na França e as máquinas ficam num hosting no Porto (Portugal). Está aprendendo programação, então os textos do painel devem ser simples e diretos.

O painel é pessoal. O repositório é público no GitHub, mas outras pessoas usarem é só um bônus. Não é um objetivo, e nenhuma decisão de produto deve ser tomada pensando nelas.

## Product Purpose

Ver e controlar, de longe, os mineradores de Bitcoin (e os aluguéis de hashrate) que ficam num hosting de terceiros, sem expor nada na internet.

Acima de tudo, o painel deve responder a uma pergunta: **estou ganhando ou perdendo dinheiro, e por quê?** Ou seja: receita esperada contra o custo de energia das máquinas próprias, e custo contra o valor esperado dos aluguéis.

Sucesso é:

- quando chega um alerta, entender o problema e agir em poucos toques;
- numa análise com calma, saber se cada máquina e cada aluguel compensa.

## Positioning

Junta numa tela só, privada e em português, o que hoje fica espalhado em vários sites:

- a leitura direta das máquinas (LuxOS e AxeOS);
- as pools (defisurfers, Parasite e Hash Commander em BCH);
- os aluguéis de hashrate;
- as finanças com a tarifa real do hosting;
- os blocos encontrados.

Tudo é calculado para as máquinas e os custos deste usuário, não para um minerador genérico.

## Operating Context

- **Fluxo principal 1, o alerta:** chega um aviso no Telegram, ele abre o painel, entende o que aconteceu e, se preciso, age pelo controle remoto.
- **Fluxo principal 2, a análise:** ele senta com calma para ver os gráficos de hashrate, o lucro e o custo de cada máquina e de cada aluguel, e os blocos encontrados, e então decide (por exemplo, manter ou cancelar um aluguel, ou mudar o perfil de potência).
- **Aparelhos de acesso:**
  - MacBook Air 2020 Intel com 8 GB, pelo Safari "Adicionar ao Dock", que fica sem memória com facilidade;
  - iPhone 12, pela Tailscale;
  - app de barra de menu no Mac (SwiftUI), que mostra o hashrate ao lado do relógio.
- **Onde o servidor roda:** hoje o painel roda no próprio Mac (launchd + `tailscale serve`). Foi comprado um Raspberry Pi 5 de 8 GB (nov/2026) para virar o servidor 24 h.
- **Ferramentas externas que ele também usa:** MiningRigRentals, os sites das pools, mempool.space e o Telegram.

## Capabilities and Constraints

**O que o painel já faz:**

- status em tempo real das máquinas, juntando a leitura local com o worker na pool, com tolerância a falhas passageiras das APIs;
- gráfico de hashrate de 24 h, 3 d, 7 d e 30 d em **linhas**. O usuário testou velas e rejeitou;
- alertas pelo Telegram. No painel, os alertas ativos aparecem **só no aviso do topo**, cada um com ×. A lista e o histórico de alertas foram removidos a pedido do usuário;
- finanças:
  - máquinas próprias: receita esperada × energia, com a tarifa do hosting configurável, hoje 0,14 €/kWh;
  - aluguéis: custo × valor esperado × chance de bloco solo;
- blocos encontrados, com os filtros Rede, Minhas pools, Solo e pequenas, e qualquer pool;
- seção Mempool, com taxa mínima, memória e não confirmadas. O Goggles e o gráfico de transações recebidas foram removidos a pedido do usuário;
- controle remoto: perfil, dormir/acordar e reiniciar no LuxOS; frequência e voltagem seguras no AxeOS. **Nunca foi testado contra hardware real.**

**Restrições técnicas:**

- escuta só em `127.0.0.1`; o acesso remoto é só pela Tailscale. LuxOS e AxeOS não têm senha, então o painel nunca pode ficar exposto na rede local;
- front-end de arquivo único (`minerador/static/index.html`, HTML + CSS + JS puro, sem framework e sem build);
- desempenho é requisito. A máquina do usuário tem pouca RAM: as animações devem ser baratas, parar fora da tela e respeitar o "modo leve";
- interface, logs e mensagens sempre em português;
- o repositório é público. Nunca mostrar endereços de carteira reais, IPs ou tokens em código, exemplos ou capturas;
- o logo da Hash Commander é carregado do site dela e não pode ser copiado para o repositório.

**Termos do domínio:** hashrate (TH/s), worker, pool, PPLNS, solo, hashprice, aluguel (MiningRigRentals), BCH (Hash Commander), perfil de potência.

**Ainda sem definição:**

- a potência real da S19j XP, que hoje está configurada como 300 W / 34 TH/s e precisa ser confirmada com o LuxOS;
- o modelo exato e o consumo da Bitaxe;
- o endereço e o worker do NerdMiner na Parasite.

## Brand Commitments

- O nome visível é **"Painel de Controle"**. O pacote Python continua se chamando `minerador`.

## Evidence on Hand

- `demo.db`, `config.demo.toml` e `tools/simulador.py`: dados e APIs simuladas para desenvolver e demonstrar sem hardware.
- `tests/`: JSON real das APIs das pools e das máquinas, usado pelos testes de parser.
- Não existem dados de controle remoto contra hardware real. Não afirmar que funciona nas máquinas.
- Os números financeiros são estimativas (receita esperada, chance de bloco). Não apresentar como lucro garantido.

## Product Principles

1. **Dinheiro primeiro.** Lucro e custo de cada máquina e de cada aluguel devem estar sempre visíveis e honestos. Estimativa é chamada de estimativa.
2. **Do alerta à ação em poucos toques.** Quem chega por um alerta deve ver logo o que está errado e o que pode fazer.
3. **Leve de verdade.** O painel não pode pesar no Mac nem no iPhone. Desempenho vale mais que efeito.
4. **Privado por padrão.** Nada exposto na internet nem na rede local, e nenhum dado real no repositório público.
5. **Português simples.** Textos curtos e claros para quem não é técnico.
