FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod .
COPY main.go .
RUN go mod tidy && go build -o exporter main.go

FROM alpine:3.18
WORKDIR /app
COPY --from=builder /app/exporter .
EXPOSE 8080
ENTRYPOINT ["./exporter"]
