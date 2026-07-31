# syntax=docker/dockerfile:1
# Build the hdh wheel, then install it into a slim runtime image.
# Quality gates (tests, coverage, ruff, mypy, security) run before this via
# `just build` — the image itself stays lean.

FROM python:3.13-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.13-slim
LABEL org.opencontainers.image.title="hdh" \
      org.opencontainers.image.description="Health Data Hub — synthetic family-medicine EHR toolkit" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/OWNER/hdh"

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir "$(ls /tmp/*.whl)[risk,agent,api]" && rm /tmp/*.whl

# Non-root; /data holds the SQLite database (mount a volume there)
RUN useradd --create-home --uid 1000 hdh && mkdir /data && chown hdh /data
USER hdh
WORKDIR /data

EXPOSE 8000
ENTRYPOINT ["hdh"]
CMD ["--help"]
