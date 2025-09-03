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

# Copia solo i file necessari per la cache di Poetry
COPY --chown=mediaflow_proxy:mediaflow_proxy pyproject.toml poetry.lock* ./

# Installa dipendenze in un layer unico e pulito
RUN pip install --no-cache-dir --user poetry \
    && poetry config virtualenvs.in-project true \
    && poetry install --no-root --only main --no-ansi --no-interaction

# Copia il resto del progetto
COPY --chown=mediaflow_proxy:mediaflow_proxy . .

USER mediaflow_proxy

EXPOSE 8888

# Usa exec form, niente "sh -c" (meno overhead e più sicuro)
CMD ["poetry", "run", "gunicorn", "mediaflow_proxy.main:app", \
     "-w", "4", "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8888", "--timeout", "120", \
     "--max-requests", "500", "--max-requests-jitter", "200", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--log-level", "info", \
     "--forwarded-allow-ips", "${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
