# Instalação no Raspberry Pi + acesso pela Tailscale

## 1. O que você precisa

* **Raspberry Pi 4 ou 5.**
  * Rode de um **SSD USB**, se possível. O banco grava a cada 15 s e cartão SD se desgasta.
  * Use fonte oficial.
* **Cabo de rede** ligando o Pi ao mesmo switch ou roteador dos mineradores, no hosting.
* Conta na **Tailscale** (o plano gratuito basta).

## 2. Preparar o Pi (em casa, antes de levar ao hosting)

1. No Mac, use o **Raspberry Pi Imager** para gravar o **Raspberry Pi OS Lite (64‑bit)**. Na engrenagem de configuração:
   * hostname: `minerador`
   * usuário: `pi` com uma senha forte
   * ative o **SSH**
2. Ligue o Pi na sua rede e entre por SSH: `ssh pi@minerador.local`
3. Instale o básico:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y python3-venv
   ```
4. Copie o projeto do Mac para o Pi. Rode no Mac, dentro da pasta do projeto:
   ```bash
   rsync -av --exclude .venv --exclude '*.db*' ./ pi@minerador.local:/home/pi/minerador/
   ```
5. No Pi:
   ```bash
   cd ~/minerador
   python3 -m venv .venv
   .venv/bin/pip install .
   ```

## 3. Tailscale no Pi

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up            # abre um link: faça login com sua conta
sudo tailscale set --ssh     # opcional: permite SSH pela Tailscale, sem expor a porta 22
```

Na **página de administração da Tailscale** (login.tailscale.com/admin):
* Em *Machines*, abra o `minerador` e **desative "Key expiry"**. Sem isso, o Pi perde o acesso depois de alguns meses.
* Em *DNS*, deixe **MagicDNS** e **HTTPS Certificates** ligados.

## 4. Configurar

```bash
cp config.example.toml config.toml
nano config.toml
```

Preencha:
* o IP da S19j XP e o do Bitaxe;
* o seu endereço BTC de cada pool;
* o nome do worker do NerdMiner.

Já no hosting, confira se o Pi alcança as máquinas:

```bash
# LuxOS (S19j XP): deve responder um JSON com "SUMMARY"
python3 -c "import socket,json;s=socket.create_connection(('IP_DA_S19',4028),5);s.send(b'{\"command\":\"summary\"}');print(s.recv(65536))"

# AxeOS (Bitaxe): deve responder um JSON com "hashRate"
curl http://IP_DO_BITAXE/api/system/info
```

> **Dica:** peça ao hosting uma **reserva de DHCP** (IP fixo) para as máquinas. Se o IP mudar, o painel perde a máquina até você corrigir o `config.toml`.

## 5. Rodar como serviço (liga sozinho no boot)

```bash
sudo cp deploy/minerador.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now minerador
systemctl status minerador          # deve estar "active (running)"
journalctl -u minerador -f          # logs ao vivo
```

## 6. Publicar o painel só para a sua tailnet

```bash
sudo tailscale serve --bg 8080
tailscale serve status              # mostra o endereço, ex.: https://minerador.SEU-TAILNET.ts.net
```

O painel escuta apenas em `127.0.0.1`. O `tailscale serve` cria um endereço **HTTPS** visível **somente** para os seus dispositivos logados na Tailscale. Nada fica aberto na rede do hosting nem na internet.

## 7. No Mac

1. Instale o app da **Tailscale** (App Store) e entre com a mesma conta.
2. Abra `https://minerador.SEU-TAILNET.ts.net` no **Safari**.
3. Menu **Arquivo → Adicionar ao Dock** (macOS Sonoma ou mais novo). O painel vira um app com ícone próprio.

No iPhone/Android: app da Tailscale + navegador → "Adicionar à Tela de Início".

## 8. (Opcional) Acessar as telas originais das máquinas

Para abrir a interface do LuxOS ou do AxeOS direto do Mac, transforme o Pi em *subnet router*:

```bash
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
sudo tailscale set --advertise-routes=192.168.1.0/24   # a sub-rede do hosting
```

Depois aprove a rota em *Machines → minerador → Edit route settings*.

> LuxOS e AxeOS **não têm senha**. Com o subnet router, qualquer dispositivo da sua tailnet consegue controlá-los. Mantenha a tailnet só com dispositivos seus.

## Atualizar o app depois

No Mac:

```bash
rsync -av --exclude .venv --exclude '*.db*' --exclude config.toml ./ pi@minerador:/home/pi/minerador/
```

No Pi:

```bash
cd ~/minerador && .venv/bin/pip install . && sudo systemctl restart minerador
```
