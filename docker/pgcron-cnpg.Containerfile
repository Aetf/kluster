# vim: set ft=Dockerfile
#
# A CNPG operand carrying pg_cron, which splitpro's schema depends on
# (declarative/workloads.md). The `standard` flavor of the base no longer
# ships barman-cloud; backups go through the barman-cloud CNPG-I plugin
# instead, so nothing here has to provide it.
ARG PG_TAG
ARG PGCRON_REV

FROM ghcr.io/cloudnative-pg/postgresql:${PG_TAG}

ARG PG_MAJOR

USER root

# pg_cron comes from the PGDG apt repo, which the base image already
# configures, and from it for the exact server major the base carries.
#
# Written as one `sh -c` line rather than a heredoc: the buildah on the runners
# parses the heredoc form as instructions and fails on the first shell builtin.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}-cron; \
    apt-get clean -y; \
    rm -rf /var/lib/apt/lists/*

# The uid the operator runs the operand as.
USER 26
