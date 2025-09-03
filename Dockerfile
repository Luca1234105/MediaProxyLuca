FROM python:3.13.5-slim AS base

# Ambiente base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.3 \
    PATH="/home/mediaflow_proxy/.local/bin:$PATH" \
    PORT=8888

WORKDIR /mediaflow_proxy

# Crea utente non-root
RUN useradd -m mediaflow_proxy

# Installa Poetry come root (così finisce in PATH globale)
RUN pip install --no-cache-dir "poetry==$POETRY_VERSION"

# Copia solo i file necessari per la cache delle dipendenze
COPY --chown=mediaflow_proxy:mediaflow_proxy pyproject.toml poetry.lock* ./

# Installa le dipendenze (main senza dev/test)
RUN poetry config virtualenvs.in-project true \
    && poetry install --no-root --only main --no-ansi --no-interaction

# Copia il resto del progetto
COPY --chown=mediaflow_proxy:mediaflow_proxy . .

# Passa all’utente non-root
USER mediaflow_proxy

EXPOSE 8888

# Usa exec form (più sicuro e meno overhead)
CMD ["poetry", "run", "gunicorn", "mediaflow_proxy.main:app", \
     "-w", "4", "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8888", "--timeout", "120", \
     "--max-requests", "500", "--max-requests-jitter", "200", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--log-level", "info", \
     "--forwarded-allow-ips", "127.0.0.1"]
