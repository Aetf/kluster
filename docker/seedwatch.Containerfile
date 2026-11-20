# vim: set ft=Dockerfile
#
# seedwatch: qBittorrent's seed lifecycle reconciler and its web UI on 8490.
# It reads the torrents and their tags through qBittorrent's API, stats the
# Downloads tree it is given read-only, and delegates every deletion to
# qBittorrent. The only files it writes are the two logs under its data
# directory, `/data`: `audit.jsonl`, every mutation it makes or would make,
# and `history.jsonl`, one line of category totals per pass. Its policy is a
# TOML file at `/config/config.toml`; without one it runs on its defaults,
# among them `dry_run`.
#
# The source is the `seedwatch` directory of Aetf/homelab-ops at a pinned
# commit, installed from its own `uv.lock` into a virtual environment. The
# project stays where the environment's editable install points, because the
# app serves its UI from `web/` beside `src/` in that tree.
FROM docker.io/library/python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d /uv /usr/local/bin/uv
ARG SEEDWATCH_COMMIT
ADD https://github.com/Aetf/homelab-ops/archive/${SEEDWATCH_COMMIT}.tar.gz /tmp/src.tar.gz
RUN mkdir /app && tar -xzf /tmp/src.tar.gz -C /app --strip-components=2 \
    "homelab-ops-${SEEDWATCH_COMMIT}/seedwatch/pyproject.toml" \
    "homelab-ops-${SEEDWATCH_COMMIT}/seedwatch/uv.lock" \
    "homelab-ops-${SEEDWATCH_COMMIT}/seedwatch/src" \
    "homelab-ops-${SEEDWATCH_COMMIT}/seedwatch/web"
# The base's interpreter, never a downloaded one, so the environment's links
# resolve in the runtime stage, which starts from the same `base`. `--locked`
# refuses a lock that has drifted from `pyproject.toml`. Bytecode is compiled
# here because the root filesystem is read-only where it runs.
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
RUN uv sync --locked --no-dev && .venv/bin/python -m compileall -q src

FROM base
COPY --from=build /app /app
WORKDIR /app
ENV PATH=/app/.venv/bin:${PATH} \
    DATA_DIR=/data \
    SEEDWATCH_CONFIG=/config/config.toml

# The user a consumer's pod runs as, which owns nothing in the image: the
# data directory is the volume the consumer mounts at `/data`. The build
# imports the app as that user on every architecture it builds, with a data
# directory of its own, and finds the UI where the app serves it from, so an
# image whose environment does not load is never published.
USER 65534:65534
RUN DATA_DIR=/tmp/seedwatch-check python -c \
    'import seedwatch.main as m; assert (m.WEB_DIR / "index.html").is_file(), m.WEB_DIR' \
    && rm -r /tmp/seedwatch-check

EXPOSE 8490
CMD ["python", "-m", "seedwatch.main"]
