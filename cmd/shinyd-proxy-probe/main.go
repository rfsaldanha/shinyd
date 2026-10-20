// A local-only experiment harness, not an installable production gateway.
package main

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"time"

	"github.com/rfsaldanha/shinyd/internal/gateway"
)

type config struct {
	Token  string `json:"token"`
	Routes []struct {
		Host   string `json:"host"`
		Target string `json:"target"`
		ID     string `json:"id"`
	} `json:"routes"`
}

func main() {
	path := flag.String("config", "", "Private JSON fixture configuration")
	bind := flag.String("bind", "127.0.0.1:8989", "Loopback listener only")
	flag.Parse()
	host, _, err := net.SplitHostPort(*bind)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		fmt.Fprintln(os.Stderr, "probe must bind to a loopback address")
		os.Exit(1)
	}
	data, err := os.ReadFile(*path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "cannot read probe config")
		os.Exit(1)
	}
	var cfg config
	if json.Unmarshal(data, &cfg) != nil || len(cfg.Token) < 32 {
		fmt.Fprintln(os.Stderr, "invalid probe config")
		os.Exit(1)
	}
	var routes []gateway.Route
	for _, item := range cfg.Routes {
		u, err := url.Parse(item.Target)
		if err != nil {
			os.Exit(1)
		}
		routes = append(routes, gateway.Route{Host: item.Host, Target: u, InstanceID: item.ID})
	}
	expected := sha256.Sum256([]byte(cfg.Token))
	auth := func(_ context.Context, r *http.Request, _ gateway.Route) error {
		cookie, err := r.Cookie("shinyd_probe")
		if err == nil {
			actual := sha256.Sum256([]byte(cookie.Value))
			if subtle.ConstantTimeCompare(actual[:], expected[:]) == 1 {
				return nil
			}
		}
		return errors.New("invalid probe credential")
	}
	handler, err := gateway.New(routes, auth, "http", time.Second)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid route configuration")
		os.Exit(1)
	}
	server := &http.Server{
		Addr: *bind, Handler: handler, ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout: time.Minute, MaxHeaderBytes: 32 * 1024,
	}
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "probe listener failed")
		os.Exit(1)
	}
}
