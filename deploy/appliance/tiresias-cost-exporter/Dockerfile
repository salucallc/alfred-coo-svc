FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY . .
RUN go build -o exporter .

FROM alpine:3.18
WORKDIR /app
COPY --from=builder /app/exporter .
EXPOSE 8080
ENTRYPOINT ["./exporter"]
