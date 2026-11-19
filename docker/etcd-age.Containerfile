# vim: set ft=Dockerfile
#
# The runtime of the etcd restore-verify: etcd's own binaries to restore a
# snapshot and serve it, age to decrypt it, and curl with busybox's shell to
# fetch it from the backup bucket. Every byte is named by a digest or verified
# by Go's checksum database.

# etcd, `etcdctl` and `etcdutl` from the image Talos runs etcd from, its
# `<tag>@<digest>` pinned in the conf. That image is distroless and the three
# binaries are static Go, so they run on the musl base below as they are.
ARG ETCD
FROM registry.k8s.io/etcd:${ETCD} AS etcd

# age, built from its module rather than fetched as a release asset: `go
# install module@version` checks the module and every dependency against
# sum.golang.org, so nothing is fetched unverified and no hash is written by
# hand. `GOTOOLCHAIN=local` makes a release that asks for a newer Go fail the
# build rather than download a toolchain nothing pins. Static, for the musl
# base.
FROM docker.io/library/golang:1.27-alpine@sha256:eb61c3873c97416fff0116553fff65d67a3549f72e11d9758849a689cdd8b523 AS age
ARG AGE_VERSION
RUN CGO_ENABLED=0 GOTOOLCHAIN=local GOBIN=/out \
    go install -trimpath "filippo.io/age/cmd/age@v${AGE_VERSION}"

# curl's image at the digest the stores' bucket client already runs from
# (`versions:image-metrics-bucket` in `Pulumi.yaml`): its busybox is the shell
# that client's script runs under, and its curl reads headers from a file.
FROM docker.io/curlimages/curl:8.23.0@sha256:3fb7e13e5d179a845c97d78204f3f1064d1907037f26a3ebf91710571151fd93
COPY --from=etcd /usr/local/bin/etcd /usr/local/bin/etcdctl /usr/local/bin/etcdutl /usr/local/bin/
COPY --from=age /out/age /usr/local/bin/age

# The user a consumer's pod runs as. The build runs each binary as that user
# on every architecture it builds and reads back its version, so an image
# whose binaries do not start, or are of another release than the conf names,
# is never published. etcd prints its release without the tag's `v`; age
# prints the module version it was built at.
ARG ETCD
ARG AGE_VERSION
USER 65534:65534
RUN set -eu; \
    release="${ETCD%%@*}"; \
    release="${release#v}"; \
    etcd --version | grep -Fx "etcd Version: ${release}"; \
    etcdctl version | grep -Fx "etcdctl version: ${release}"; \
    etcdutl version | grep -Fx "etcdutl version: ${release}"; \
    age --version | grep -Fx "v${AGE_VERSION}"
