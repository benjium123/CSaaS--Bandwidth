FROM golang:1.24-alpine AS builder
ENV GOTOOLCHAIN=auto
WORKDIR /src
COPY . .
RUN go get github.com/livekit/server-sdk-go/v2@v2.18.1 && go mod tidy && go vet ./... && go test ./... && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /lkrec .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=builder /lkrec /lkrec
ENTRYPOINT ["/lkrec"]
