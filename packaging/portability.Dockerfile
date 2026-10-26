# Toolchain only. Source arrives over stdin; no credentials or owner directory is copied.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs \
    && rm -rf /var/lib/apt/lists/*
USER 65532:65532
WORKDIR /work
