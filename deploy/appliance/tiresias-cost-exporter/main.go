package main

import (
    "log"
    "net/http"
    "time"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
    tokens = prometheus.NewCounterVec(prometheus.CounterOpts{
        Name: "mc_tokens_total",
        Help: "Total tokens per tenant/persona/model/provider",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
    cost = prometheus.NewCounterVec(prometheus.CounterOpts{
        Name: "mc_cost_usd_total",
        Help: "Total cost in USD per tenant/persona/model/provider",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
)

func init() {
    prometheus.MustRegister(tokens, cost)
}

func scrape() {
    // Placeholder implementation – in production this would query tiresias_audit_log.
    tokens.WithLabelValues("tenantA", "personaX", "gpt-4", "openai").Add(100)
    cost.WithLabelValues("tenantA", "personaX", "gpt-4", "openrouter").Add(0.05)
}

func main() {
    go func() {
        for {
            scrape()
            time.Sleep(60 * time.Second)
        }
    }()
    http.Handle("/metrics", promhttp.Handler())
    log.Fatal(http.ListenAndServe(":8080", nil))
}
