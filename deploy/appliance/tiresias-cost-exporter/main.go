package main

import (
    "log"
    "net/http"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
    tokens = prometheus.NewGaugeVec(prometheus.GaugeOpts{
        Name: "mc_tokens_total",
        Help: "Total tokens per tenant/persona/model/provider",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
    cost = prometheus.NewGaugeVec(prometheus.GaugeOpts{
        Name: "mc_cost_usd_total",
        Help: "Total cost in USD per tenant/persona/model/provider",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
)

func init() {
    prometheus.MustRegister(tokens)
    prometheus.MustRegister(cost)
    // Placeholder zero values to ensure metrics are exposed even without data.
    tokens.WithLabelValues("unknown", "unknown", "unknown", "unknown").Set(0)
    cost.WithLabelValues("unknown", "unknown", "unknown", "unknown").Set(0)
}

func main() {
    http.Handle("/metrics", promhttp.Handler())
    log.Println("tiresias-cost-exporter listening on :9100 /metrics")
    if err := http.ListenAndServe(":9100", nil); err != nil {
        log.Fatalf("server failed: %v", err)
    }
}
