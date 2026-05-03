package main

import (
    "net/http"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
    tokensTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
        Name: "mc_tokens_total",
        Help: "Total tokens processed, labeled by tenant, persona, model, provider.",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
    costTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
        Name: "mc_cost_usd_total",
        Help: "Total cost in USD, labeled by tenant, persona, model, provider.",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
)

func init() {
    prometheus.MustRegister(tokensTotal, costTotal)
    // In a real exporter, scrape tiresias_audit_log here.
}

func main() {
    http.Handle("/metrics", promhttp.Handler())
    // Listen on :8080
    http.ListenAndServe(":8080", nil)
}
