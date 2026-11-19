# Instalação num PC com Windows + acesso pela Tailscale

O painel roda direto no Windows, sem Linux nem WSL. Ele consome cerca de 50 MB de RAM, então qualquer PC dá conta. Um script cuida da instalação e deixa o painel **ligando sozinho com o Windows, mesmo sem ninguém logado**.

## 1. O que você precisa

* **Um PC com Windows 10 ou 11**, que vai ficar **no hosting, na mesma rede dos mineradores**.
  * O painel lê a S19 (LuxOS) e o Bitaxe direto pela rede local. Longe deles, ele só enxerga as pools.
* **Cabo de rede** ligando o PC ao mesmo switch ou roteador das máquinas.
* Conta na **Tailscale** (o plano gratuito basta).

## 2. Ajustes do PC (faça uma vez)

* **BIOS:** procure a opção "Restore on AC Power Loss" ou "After Power Failure" e coloque em **Power On**. Assim o PC liga sozinho quando a energia volta.
* **Windows Update:** em *Configurações → Windows Update → Opções avançadas → Horário ativo*, deixe o Windows escolher automaticamente ou defina o maior período. Se o Windows reiniciar para atualizar, o painel volta sozinho.
* **Suspensão:** o script de instalação já configura o PC para **não suspender nem hibernar** quando estiver na tomada.

## 3. Copiar o projeto para o PC

1. No GitHub, abra o repositório **Painel-de-Controle**, clique em **Code → Download ZIP** e extraia.
2. Renomeie a pasta extraída para `PainelDeControle` e coloque em `C:\`. O resultado é `C:\PainelDeControle`.
3. Copie o seu **`config.toml`** do Mac para dentro dessa pasta (pendrive, e-mail para você mesmo etc.).
   * Ele não está no GitHub de propósito, porque tem seus endereços.
   * Se pular este passo, o script cria um a partir do exemplo e abre no Bloco de Notas para você preencher.

## 4. Instalar

1. Abra `C:\PainelDeControle\deploy\windows`.
2. Clique com o botão direito em **`painel.ps1`** e escolha **"Executar com o PowerShell"**.
3. Aceite o pedido de administrador.

O script faz tudo sozinho, em 6 passos:

| Passo | O que faz |
|---|---|
| 1. Python | Usa o Python 3.11+ que já existir; se não houver, instala o Python 3.13 pelo winget |
| 2. Parar | Para o painel, se ele já estiver rodando (útil ao atualizar) |
| 3. Ambiente | Cria a pasta `.venv` e instala o painel e as dependências |
| 4. Configuração | Confere o `config.toml` e abre no Bloco de Notas se houver erro |
| 5. Inicialização | Cria a tarefa **"Painel de Controle"** no Agendador de Tarefas (liga com o Windows, sem login) e sobe o painel |
| 6. Tailscale | Oferece instalar a Tailscale e publica o painel para os seus dispositivos |

> Se o Windows bloquear o script dizendo que ele veio da internet, clique com o botão direito no `painel.ps1` → **Propriedades** → marque **Desbloquear** → OK, e tente de novo.

No final, o painel estará em `http://127.0.0.1:8080` no próprio PC.

## 5. Tailscale

Se o script instalou a Tailscale agora, faça login e **rode o `painel.ps1` de novo** para ele publicar o painel.

1. Clique no ícone da Tailscale perto do relógio → **Log in** e entre com a sua conta.
2. No mesmo menu: **Preferences → Run unattended** (ligado). Sem isso, a Tailscale só funciona com alguém logado no Windows.
3. Na **página de administração** (login.tailscale.com/admin):
   * em *Machines*, abra este PC e escolha **Disable key expiry**. Sem isso, o PC perde o acesso depois de alguns meses;
   * em *DNS*, deixe **MagicDNS** e **HTTPS Certificates** ligados.

O painel escuta apenas em `127.0.0.1`. O script roda `tailscale serve`, que cria um endereço **HTTPS** visível **somente** para os seus dispositivos logados na Tailscale, algo como `https://nome-do-pc.SEU-TAILNET.ts.net`. Nada fica aberto na rede do hosting nem na internet, e não é preciso mexer no firewall.

## 6. Conferir se o PC alcança as máquinas

Já no hosting, abra o **PowerShell** e teste:

```powershell
# LuxOS (S19j XP): deve aparecer "TcpTestSucceeded : True"
Test-NetConnection IP_DA_S19 -Port 4028

# AxeOS (Bitaxe): deve mostrar os dados do Bitaxe (hashRate, temp…)
Invoke-RestMethod http://IP_DO_BITAXE/api/system/info
```

> **Dica:** peça ao hosting uma **reserva de DHCP** (IP fixo) para as máquinas. Se o IP mudar, o painel perde a máquina até você corrigir o `config.toml`.

## 7. No Mac

1. Instale o app da **Tailscale** (App Store) e entre com a mesma conta.
2. Abra o endereço `https://….ts.net` que o script mostrou, no **Safari**.
3. Menu **Arquivo → Adicionar ao Dock**. O painel vira um app com ícone próprio.

No iPhone/Android: app da Tailscale + navegador → "Adicionar à Tela de Início".

## Comandos do dia a dia

Abra o PowerShell na pasta `C:\PainelDeControle\deploy\windows` e rode:

| Comando | Para quê |
|---|---|
| `.\painel.ps1 status` | Ver se o painel está rodando e o endereço da Tailscale |
| `.\painel.ps1 reiniciar` | Reiniciar (por exemplo, depois de editar o `config.toml`) |
| `.\painel.ps1 log` | Ver o log ao vivo (Ctrl+C para sair) |
| `.\painel.ps1 atualizar` | Baixar a versão mais nova do GitHub e reinstalar |
| `.\painel.ps1 parar` | Parar o painel até o próximo boot |
| `.\painel.ps1 remover` | Tirar o painel da inicialização (não apaga nada) |

Se o PowerShell recusar o comando por causa da "política de execução", use assim:
`powershell -ExecutionPolicy Bypass -File .\painel.ps1 status`

O log fica em `C:\PainelDeControle\minerador.log`. Quando passa de 10 MB, ele vira `minerador.log.old` e um novo começa.

## Atualizar

`.\painel.ps1 atualizar` baixa a versão mais nova do GitHub e reinstala. O seu `config.toml` e o histórico (`minerador.db`) ficam intactos.

## Mexer no PC de longe

O painel não precisa disso, mas para mexer no Windows do hosting sem ir até lá:

* **Windows Pro:** ative a *Área de Trabalho Remota* e conecte pelo endereço da Tailscale com o app **Windows App** (antigo Microsoft Remote Desktop) no Mac.
* **Windows Home:** não tem Área de Trabalho Remota. Use o **Chrome Remote Desktop** (gratuito).
