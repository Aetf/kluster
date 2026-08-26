# vim: set ft=Dockerfile
#
# The CNPG operand immich's database runs on: the stock base plus the two
# vector extensions its schema loads, VectorChord (`vchord`) and pgvecto.rs
# (`vectors`). Both are prebuilt .deb packages picked per architecture —
# pgvecto.rs ships its .deb inside a per-architecture image, VectorChord as a
# release asset — so nothing here compiles Rust, and the arm64 build costs the
# same as the amd64 one.
ARG PG_MAJOR
ARG PG_TAG
ARG PGVECTO_RS_SEMVER
ARG VECTORCHORD_SEMVER
# Supplied by the build, not by the conf: under a native build the runner
# decides the architecture.
ARG TARGETARCH

FROM docker.io/tensorchord/pgvecto-rs-binary:pg${PG_MAJOR}-v${PGVECTO_RS_SEMVER}-${TARGETARCH} AS pgvecto-binary

FROM ghcr.io/cloudnative-pg/postgresql:${PG_TAG}

ARG PG_MAJOR
ARG VECTORCHORD_SEMVER
ARG TARGETARCH

USER root

COPY --from=pgvecto-binary /pgvecto-rs-binary-release.deb /tmp/vectors.deb

# One `sh -c` line rather than a heredoc: the buildah on the runners parses the
# heredoc form as instructions and fails on the first shell builtin.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends wget; \
    apt-get install -y --no-install-recommends /tmp/vectors.deb; \
    wget -q -O /tmp/vchord.deb \
      "https://github.com/tensorchord/VectorChord/releases/download/${VECTORCHORD_SEMVER}/postgresql-${PG_MAJOR}-vchord_${VECTORCHORD_SEMVER}-1_${TARGETARCH}.deb"; \
    dpkg -i /tmp/vchord.deb; \
    rm -f /tmp/vectors.deb /tmp/vchord.deb; \
    apt-get purge -y wget; \
    apt-get autoremove -y; \
    apt-get clean -y; \
    rm -rf /var/lib/apt/lists/*

# The base of this line runs postgres under a different uid than the operator
# expects; the extensions are installed as root, the server is not.
RUN usermod -u 26 postgres
USER 26
