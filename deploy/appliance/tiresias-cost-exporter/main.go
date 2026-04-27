package main

import (
    "context"
    "log"
    "net/http"
    "time"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
    tokensGauge = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{Name: "mc_tokens_total", Help: "Total tokens per tenant/persona/model/provider"},
        []string{"tenant_id", "persona_id", "model", "provider"},
    )
    costGauge = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{Name: "mc_cost_usd_total", Help: "Total cost in USD per tenant/persona/model/provider"},
        []string{"tenant_id", "persona_id", "model", "provider"},
    )
)

func init() {
    prometheus.MustRegister(tokensGauge, costGauge)
}

func scrapeMetrics(ctx context.Context) {
    // Placeholder implementation: emit dummy values every 60s.
    ticker := time.NewTicker(60 * time.Second)
    defer ticker.Stop()
    for {
        select {
        case <-ctx.Done():
            return
        case <-ticker.C:
            // In a real exporter, query tiresias_audit_log here.
            tokensGauge.WithLabelValues("tenantA", "personaX", "gpt-4", "openrouter").Set(12345)
            costGauge.WithLabelValues("tenantA", "personaX", "gpt-4", "openrouter").Set(12.34)
        }
    }
}

func main() {
    ctx, cancel := context.WithCancel(context.Background())
    defer cancel()
    go scrapeMetrics(ctx)

    http.Handle("/metrics", promhttp.Handler())
    log.Println("starting exporter on :8080")
    if err := http.ListenAndServe(":8080", nil); err != nil {
        log.Fatalf("server failed: %v", err)
    }
}
