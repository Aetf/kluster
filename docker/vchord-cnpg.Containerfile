# vim: set ft=Dockerfile
#
# The CNPG operand immich's database runs on: the stock base plus the two
# vector extensions its schema loads, VectorChord (`vchord`) and pgvecto.rs
# (`vectors`). Both are prebuilt .deb packages picked per architecture —
# pgvecto.rs ships its .deb inside a per-architecture image, VectorChord as a
# release asset — so nothing here compiles Rust, and the arm64 build costs the
# same as the amd64 one.
ARG PG_TAG
ARG PG_DIGEST
# This architecture's `<tag>@<digest>`, which the build derives from the conf's
# PGVECTO_RS_AMD64 or PGVECTO_RS_ARM64.
ARG PGVECTO_RS

FROM docker.io/tensorchord/pgvecto-rs-binary:${PGVECTO_RS} AS pgvecto-binary

FROM ghcr.io/cloudnative-pg/postgresql:${PG_TAG}@${PG_DIGEST}

ARG PG_MAJOR
ARG VECTORCHORD_SEMVER
# Supplied by the build, not by the conf: under a native build the runner
# decides the architecture.
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
