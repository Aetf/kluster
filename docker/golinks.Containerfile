# vim: set ft=Dockerfile
#
# kellegous/go: a go/<name> shortlink service with a web UI (go/edit/<name>)
# and a leveldb data directory. Upstream publishes no releases and its Docker
# Hub image is stale, so this builds from a pinned commit. The Vue UI is built
# first and embedded into the Go binary (go:embed), which leaves the final
# image a single static binary on scratch.
FROM docker.io/library/alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS src
ARG GOLINKS_COMMIT
ADD https://github.com/kellegous/go/archive/${GOLINKS_COMMIT}.tar.gz /tmp/src.tar.gz
RUN mkdir /src && tar -xzf /tmp/src.tar.gz -C /src --strip-components=1

FROM docker.io/library/node:24-alpine@sha256:116b800c3754b440ee0aa3ed2f20f5bf3d99a554bfd9adeea09ea8017f6e85f5 AS ui
COPY --from=src /src /src
WORKDIR /src
RUN npm ci && npm run build

FROM docker.io/library/golang:1.27-alpine@sha256:eb61c3873c97416fff0116553fff65d67a3549f72e11d9758849a689cdd8b523 AS build
COPY --from=ui /src /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -o bin/go ./cmd/go

FROM scratch
COPY --from=build /src/bin/go /go
EXPOSE 8067
ENTRYPOINT ["/go"]
CMD ["--data=/data"]
