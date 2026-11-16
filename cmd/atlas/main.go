package main

import (
	"atlas-render/internal/capacity"
	"atlas-render/internal/engine"
	"atlas-render/internal/httpapi"
	"atlas-render/internal/persist"
	"atlas-render/internal/service"
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	if e := run(); e != nil {
		log.Print(e)
		os.Exit(1)
	}
}
func run() error {
	addr := os.Getenv("ATLAS_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8763"
	}
	path := os.Getenv("ATLAS_STATE")
	if path == "" {
		path = "var/atlas-state.json"
	}
	limits, e := capacity.FromEnv()
	if e != nil {
		return e
	}
	mem := limits.ApplyMemory()
	log.Printf("capacity gates: %s", limits)
	log.Printf("runtime bounds: GOMAXPROCS=%d memoryLimitBytes=%d", capacity.CurrentRuntime().GOMAXPROCS, mem)
	st, e := persist.Open(path)
	if e != nil {
		return e
	}
	defer st.Close()
	worker := engine.New(st, limits)
	if e = worker.Recover(); e != nil {
		return e
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	done := make(chan struct{})
	go func() { defer close(done); worker.Run(ctx) }()
	srv := &http.Server{Addr: addr, Handler: (httpapi.API{Service: &service.Service{Store: st, Limits: limits}, Worker: worker}).Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 20 * time.Second, WriteTimeout: 60 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16384}
	errc := make(chan error, 1)
	go func() { log.Printf("Atlas Render listening on %s", addr); errc <- srv.ListenAndServe() }()
	select {
	case e = <-errc:
		stop()
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdown)
	<-done
	if e == http.ErrServerClosed {
		return nil
	}
	return e
}
