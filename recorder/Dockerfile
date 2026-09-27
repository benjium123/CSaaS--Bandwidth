FROM golang:1.24-alpine AS builder
ENV GOTOOLCHAIN=auto
# libopus (cgo) decodes the callee's audio so the consent announcement waits for speech.
RUN apk add --no-cache build-base pkgconf opus-dev
WORKDIR /src
COPY . .
RUN go get github.com/livekit/server-sdk-go/v2@v2.18.1 gopkg.in/hraban/opus.v2@latest && go mod tidy     && go vet -tags nolibopusfile ./... && go test -tags nolibopusfile ./...     && CGO_ENABLED=1 go build -tags nolibopusfile -trimpath -ldflags="-s -w" -o /lkrec .

FROM alpine:3.22
RUN apk add --no-cache opus ca-certificates && adduser -D -u 65532 nonroot
COPY --from=builder /lkrec /lkrec
USER nonroot
ENTRYPOINT ["/lkrec"]
