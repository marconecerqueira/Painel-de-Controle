#!/bin/sh
# Arranque do contêiner: na primeira vez copia o exemplo para /data/config.toml (edite-o com os seus dados).
# Escuta em 0.0.0.0 só DENTRO da rede do Docker: no Umbrel quem chega até aqui é o app_proxy, que pede a
# senha do Umbrel antes. Não publique a porta 8080 direto na rede local (LuxOS e AxeOS não têm senha).
set -e
if [ ! -f /data/config.toml ]; then
    cp /app/config.example.toml /data/config.toml
    echo "Primeira vez: criei /data/config.toml a partir do exemplo. Edite-o com as suas máquinas e reinicie o app."
fi
exec minerador --config /data/config.toml --host 0.0.0.0 --port 8080
