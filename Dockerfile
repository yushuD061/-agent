FROM python:3.11.9-slim-bookworm

ARG UV_VERSION=0.11.28

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PADDLE_PDX_CACHE_HOME=/app/.paddlex \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       libgl1 \
       libglib2.0-0 \
       libgomp1 \
       libsm6 \
       libxext6 \
       libxrender1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && groupadd --gid 10001 nanoclaw \
    && useradd --uid 10001 --gid nanoclaw --create-home --shell /usr/sbin/nologin nanoclaw

WORKDIR /app

# Install the locked runtime first so source-only changes reuse the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=nanoclaw:nanoclaw . .
RUN mkdir -p /app/workspace /app/data \
    && chown -R nanoclaw:nanoclaw /app/workspace /app/data

USER 10001:10001

EXPOSE 8765 8766 8767

CMD ["/app/.venv/bin/python", "main.py"]

