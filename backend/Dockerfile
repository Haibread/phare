# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /usr/local/src/phare
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime
RUN groupadd --system --gid 1000 phare \
 && useradd --system --uid 1000 --gid phare --home /etc/phare --shell /usr/sbin/nologin phare
WORKDIR /usr/local/src/phare
COPY --from=builder --chown=phare:phare /usr/local/src/phare /usr/local/src/phare
ENV PATH="/usr/local/src/phare/.venv/bin:$PATH"
USER phare
EXPOSE 8000
ENTRYPOINT ["phare"]
CMD ["serve"]
