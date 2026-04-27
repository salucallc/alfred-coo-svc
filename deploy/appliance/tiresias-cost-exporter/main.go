package main

import (
    "database/sql"
    "log"
    "net/http"
    "time"

    _ "github.com/lib/pq"
    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

type exporter struct {
    db                 *sql.DB
    tokensGauge        *prometheus.GaugeVec
    costGauge          *prometheus.GaugeVec
    scrapeIntervalSecs int
}

func newExporter(db *sql.DB) *exporter {
    return &exporter{
        db: db,
        tokensGauge: prometheus.NewGaugeVec(prometheus.GaugeOpts{
            Name: "mc_tokens_total",
            Help: "Total tokens processed per tenant/persona/model/provider",
        }, []string{"tenant_id", "persona_id", "model", "provider"}),
        costGauge: prometheus.NewGaugeVec(prometheus.GaugeOpts{
            Name: "mc_cost_usd_total",
            Help: "Total cost in USD per tenant/persona/model/provider",
        }, []string{"tenant_id", "persona_id", "model", "provider"}),
        scrapeIntervalSecs: 60,
    }
}

func (e *exporter) collectMetrics() {
    rows, err := e.db.Query(`SELECT tenant_id, persona_id, model, provider, tokens, cost_usd FROM tiresias_audit_log`)
    if err != nil {
        log.Printf("query audit log: %v", err)
        return
    }
    defer rows.Close()
    for rows.Next() {
        var tenant, persona, model, provider string
        var tokens int64
        var cost float64
        if err := rows.Scan(&tenant, &persona, &model, &provider, &tokens, &cost); err != nil {
            log.Printf("scan row: %v", err)
            continue
        }
        e.tokensGauge.WithLabelValues(tenant, persona, model, provider).Add(float64(tokens))
        e.costGauge.WithLabelValues(tenant, persona, model, provider).Add(cost)
    }
    if err := rows.Err(); err != nil {
        log.Printf("row iteration error: %v", err)
    }
}

func (e *exporter) start() {
    ticker := time.NewTicker(time.Duration(e.scrapeIntervalSecs) * time.Second)
    go func() {
        for range ticker.C {
            e.collectMetrics()
        }
    }()
}

func main() {
    // DB connection placeholder – environment variables expected
    connStr := "postgres://user:password@localhost:5432/tiresias?sslmode=disable"
    db, err := sql.Open("postgres", connStr)
    if err != nil {
        log.Fatalf("open db: %v", err)
    }
    defer db.Close()

    exp := newExporter(db)
    prometheus.MustRegister(exp.tokensGauge)
    prometheus.MustRegister(exp.costGauge)

    exp.start()
    http.Handle("/metrics", promhttp.Handler())
    log.Println("starting metrics exporter on :9090")
    if err := http.ListenAndServe(":9090", nil); err != nil {
        log.Fatalf("listen: %v", err)
    }
}
