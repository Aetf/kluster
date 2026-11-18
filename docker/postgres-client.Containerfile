# vim: set ft=Dockerfile
#
# The runtime for this repository's Python that runs in a cluster job against
# Postgres: the interpreter at the line `.python-version` names, on the base
# `oci-sdk.Containerfile` uses, and the Postgres client of the state backend's
# major. It installs no Python package and carries no code of this repository;
# a consumer supplies its program at run time. A renovate bound keeps the base
# on that line, and the base moves with the bound in the pull request that
# moves `.python-version` (framework/ci.md §4).
FROM docker.io/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# The client the base's Debian release carries for the major, without its
# recommended packages. Its own `bin` comes first on `PATH`, so `psql` is the
# client itself rather than `postgresql-common`'s wrapper, which picks a
# version and reads a configuration of its own.
ARG POSTGRES_MAJOR
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-${POSTGRES_MAJOR} \
    && rm -rf /var/lib/apt/lists/*
ENV PATH=/usr/lib/postgresql/${POSTGRES_MAJOR}/bin:${PATH}

# The user a consumer's pod runs as. The build runs the client as that user on
# every architecture it builds and reads back its major, so an image whose
# client does not start, or is of another major than the tag says, is never
# published.
USER 65534:65534
RUN psql --version | grep -F "(PostgreSQL) ${POSTGRES_MAJOR}."
