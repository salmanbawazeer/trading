FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY scalper ./scalper
RUN pip install --no-cache-dir ".[jev]" \
 && useradd --create-home --uid 10001 scalper \
 && mkdir -p /data && chown -R scalper:scalper /data /app

USER scalper
VOLUME ["/data"]
EXPOSE 9108

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9108/healthz', timeout=3).status==200 else 1)"

# Mode comes from the MODE env var (record | backtest | paper | live).
ENTRYPOINT ["python", "-m", "scalper"]
