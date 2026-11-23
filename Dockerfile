FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# tzdata is required: window math uses the real IANA America/New_York rules.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backupdesk ./backupdesk
COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini ./

RUN useradd --system --uid 10001 --home /app desk \
    && mkdir -p /data \
    && chown -R desk:desk /app /data
USER desk

ENV DATA_DIR=/data \
    LEDGER_DB=/data/ledger/ops_ledger.sqlite3 \
    BACKUP_DIR=/data/backups \
    STATE_DB=/data/state/backupdesk.sqlite3 \
    LOCAL_TZ=America/New_York \
    HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["python", "-m", "backupdesk"]
