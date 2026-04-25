# OTEL Collector

This service runs the OpenTelemetry Collector configured to receive OTLP data on gRPC (4317) and HTTP (4318) and forward it to Prometheus and Loki.

## Configuration
The collector configuration is defined in `otel-collector-config.yaml`.

## Running
The collector is started via `docker-compose` as part of the appliance stack.

## Verification
Run:
```
curl http://localhost:4318/v1/traces -d '{}'
```
Expect a `200` response.
