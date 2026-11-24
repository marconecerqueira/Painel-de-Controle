# Imagem do Painel de Controle (+ Leda) para o Umbrel e qualquer Docker. Leve: Python + FastAPI, sem build de front-end.
# A configuração NÃO vai na imagem: fica em /data/config.toml (volume), junto com o banco SQLite.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY minerador ./minerador
RUN pip install --no-cache-dir . && rm -rf /root/.cache
COPY config.example.toml deploy/umbrel/iniciar.sh ./
RUN chmod 755 iniciar.sh
VOLUME /data
EXPOSE 8080
CMD ["/app/iniciar.sh"]
