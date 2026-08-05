# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip setuptools wheel
COPY app ./app
RUN /opt/venv/bin/pip install ".[vector,streams,storage,ocr,oidc,vault]"

FROM base AS runtime
# tesseract powers the KYC OCR tool; libmagic assists content sniffing
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr libtesseract-dev curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin finops
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /srv
COPY --chown=finops:finops app ./app
COPY --chown=finops:finops alembic ./alembic
COPY --chown=finops:finops alembic.ini pyproject.toml ./
RUN mkdir -p /srv/var/artifacts && chown -R finops:finops /srv/var
USER finops
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://localhost:8000/health/live || exit 1
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]
