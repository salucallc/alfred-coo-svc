FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod .
COPY main.go .
RUN go mod download && go build -o exporter .

FROM alpine:3.19
COPY --from=builder /app/exporter /exporter
EXPOSE 9100
ENTRYPOINT ["/exporter"]
