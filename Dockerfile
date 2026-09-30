# YANTRA server image (SPEC §21). Multi-stage: web dashboard build -> python runtime.
# Build context = repository root. Hermetic once the base layers are pulled; at install
# time on the air-gapped host the image arrives inside the bundle tar (no registry pulls).

# ---- stage 1: dashboard ------------------------------------------------------------
FROM node:22-alpine AS web-build
WORKDIR /build
COPY web/package.json web/package-lock.json* ./web/
RUN cd web && npm install --no-audit --no-fund
COPY web ./web
COPY scripts/export_schemas.py ./scripts/export_schemas.py
RUN cd web && npm run build -- --outDir /build/static --emptyOutDir

# ---- stage 2: server ---------------------------------------------------------------
FROM python:3.12-slim AS server
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    YANTRA_ASSETS_DIR=/opt/yantra
WORKDIR /opt/yantra

# System deps: git absent by design (sealed); libgl for opencv-headless is not needed;
# fonts for report rendering; tini as PID 1 so engine children reap cleanly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install the server package with the serving-host extras. When a wheelhouse/ directory is
# present in the build context (bundle build), install offline from it; otherwise from PyPI
# (connected build machine only — never on the plant host).
COPY pyproject.toml README.md ./
COPY server ./server
COPY wheelhouse* ./wheelhouse/
RUN if [ -n "$(ls wheelhouse 2>/dev/null)" ]; then \
        pip install --no-index --find-links wheelhouse ".[knowledge,vision,render,analysis,ocr]"; \
    else \
        pip install ".[knowledge,vision,render,analysis,ocr]"; \
    fi && rm -rf wheelhouse

# Assets the server reads at runtime (agents, knowledge packs, templates, skills, profiles).
COPY agents ./agents
COPY knowledge ./knowledge
COPY templates ./templates
COPY skills ./skills
COPY models/registry.yaml models/routing.yaml models/catalog.json ./models/
COPY models/profiles ./models/profiles
COPY tools ./tools
COPY corpus ./corpus
COPY scripts/seal_nftables.sh ./scripts/seal_nftables.sh
COPY --from=web-build /build/static ./server/yantra_server/static

# Models and data are volumes; the seal allowlist covers only loopback + RFC1918.
VOLUME ["/models", "/data"]
ENV YANTRA_PATHS__MODELS_DIR=/models \
    YANTRA_PATHS__DATA_DIR=/data \
    YANTRA_SEALED=1

EXPOSE 7331
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7331/api/health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["yantra", "serve", "--bind", "0.0.0.0"]
