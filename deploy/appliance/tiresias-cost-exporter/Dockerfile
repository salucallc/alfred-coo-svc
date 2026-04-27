FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod .
COPY main.go .
RUN go mod tidy && go build -o exporter .

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/exporter .
EXPOSE 9090
ENTRYPOINT ["./exporter"]
