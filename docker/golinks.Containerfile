# vim: set ft=Dockerfile
#
# kellegous/go: a go/<name> shortlink service with a web UI (go/edit/<name>)
# and a leveldb data directory. Upstream publishes no releases and its Docker
# Hub image is stale, so this builds from a pinned commit. The Vue UI is built
# first and embedded into the Go binary (go:embed), which leaves the final
# image a single static binary on scratch.
FROM docker.io/library/alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 AS src
ARG GOLINKS_COMMIT
ADD https://github.com/kellegous/go/archive/${GOLINKS_COMMIT}.tar.gz /tmp/src.tar.gz
RUN mkdir /src && tar -xzf /tmp/src.tar.gz -C /src --strip-components=1

FROM docker.io/library/node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS ui
COPY --from=src /src /src
WORKDIR /src
RUN npm ci && npm run build

FROM docker.io/library/golang:1.26-alpine@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c AS build
COPY --from=ui /src /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -o bin/go ./cmd/go

FROM scratch
COPY --from=build /src/bin/go /go
EXPOSE 8067
ENTRYPOINT ["/go"]
CMD ["--data=/data"]
