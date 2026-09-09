FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md LICENSE NOTICE.md ./
COPY sipgram ./sipgram
RUN pip install --no-cache-dir --no-deps .

RUN useradd -m -u 1000 sipgram && mkdir -p /app/config /app/sessions && chown -R sipgram:sipgram /app
USER sipgram
VOLUME ["/app/config", "/app/sessions"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD sipgram -c /app/config/config.yaml status || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "sipgram", "-c", "/app/config/config.yaml"]
CMD ["run"]
