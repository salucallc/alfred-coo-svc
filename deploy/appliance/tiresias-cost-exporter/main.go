package main

import (
    "context"
    "log"
    "net/http"
    "time"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

// Define metrics
var (
    tokensTotal = prometheus.NewGaugeVec(prometheus.GaugeOpts{
        Name: "mc_tokens_total",
        Help: "Total tokens per tenant/persona/model/provider",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
    costUSDTotal = prometheus.NewGaugeVec(prometheus.GaugeOpts{
        Name: "mc_cost_usd_total",
        Help: "Total cost in USD per tenant/persona/model/provider",
    }, []string{"tenant_id", "persona_id", "model", "provider"})
)

func init() {
    prometheus.MustRegister(tokensTotal, costUSDTotal)
}

// mock scrape function – replace with real query to tiresias_audit_log
func scrapeAuditLog(ctx context.Context) error {
    // Example static values; real implementation should query the audit log.
    tokensTotal.WithLabelValues("tenant1", "personaA", "gpt-4", "openai").Set(12345)
    costUSDTotal.WithLabelValues("tenant1", "personaA", "gpt-4", "openai").Set(12.34)
    return nil
}

func main() {
    ctx := context.Background()
    go func() {
        ticker := time.NewTicker(60 * time.Second)
        defer ticker.Stop()
        for {
            if err := scrapeAuditLog(ctx); err != nil {
                log.Printf("scrape error: %v", err)
            }
            <-ticker.C
        }
    }()

    http.Handle("/metrics", promhttp.Handler())
    log.Println("starting exporter on :8080/metrics")
    if err := http.ListenAndServe(":8080", nil); err != nil {
        log.Fatalf("listen error: %v", err)
    }
}
