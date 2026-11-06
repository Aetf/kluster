# vim: set ft=Dockerfile
#
# kellegous/go: a go/<name> shortlink service with a web UI (go/edit/<name>)
# and a leveldb data directory. Upstream publishes no releases and its Docker
# Hub image is stale, so this builds from a pinned commit. The Vue UI is built
# first and embedded into the Go binary (go:embed), which leaves the final
# image a single static binary on scratch.
FROM docker.io/library/alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a AS src
ARG GOLINKS_COMMIT
ADD https://github.com/kellegous/go/archive/${GOLINKS_COMMIT}.tar.gz /tmp/src.tar.gz
RUN mkdir /src && tar -xzf /tmp/src.tar.gz -C /src --strip-components=1

FROM docker.io/library/node:24-alpine@sha256:a34ab6d7f6f4813f4d0c5469d74685604b962fbdbf5793a7bc7b4998ecb867c8 AS ui
COPY --from=src /src /src
WORKDIR /src
RUN npm ci && npm run build

FROM docker.io/library/golang:1.27-alpine@sha256:fa3d596898d9f126af7c01d14ee7597bab3f39cccf392ef36b69fe77cd681f18 AS build
COPY --from=ui /src /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -o bin/go ./cmd/go

FROM scratch
COPY --from=build /src/bin/go /go
EXPOSE 8067
ENTRYPOINT ["/go"]
CMD ["--data=/data"]
