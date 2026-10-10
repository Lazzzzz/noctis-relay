# Noctis relay: stdlib-only Python service, no third-party dependencies.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NOCTIS_HOST=0.0.0.0 \
    NOCTIS_PORT=8787

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin noctis
WORKDIR /app
COPY noctis_relay/ ./noctis_relay/

USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2)"]
CMD ["python", "-m", "noctis_relay"]
