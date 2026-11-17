# vim: set ft=Dockerfile
#
# The runtime for this repository's Python that runs in a cluster job: the
# interpreter at the line `.python-version` names, and the OCI SDK. It carries
# no code of this repository; a consumer supplies its program at run time. A
# renovate bound keeps the base on that line, and the base moves with the
# bound in the pull request that moves `.python-version` (framework/ci.md §4).
FROM docker.io/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ARG OCI_VERSION
RUN pip install --no-cache-dir oci==${OCI_VERSION}
