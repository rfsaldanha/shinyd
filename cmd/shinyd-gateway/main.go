// The data gateway listens only on loopback; an HTTPS ingress terminates TLS.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/rfsaldanha/shinyd/internal/agent"
	"github.com/rfsaldanha/shinyd/internal/gateway"
)

func run() error {
	bind := flag.String("bind", "127.0.0.1:9090", "Listener loopback, atrás da entrada HTTPS")
	activated := flag.Bool("socket-activation", false, "Usa somente o socket Unix entregue pelo systemd")
	socket := flag.String("control-socket", "/run/shinyd/gateway-auth.sock", "Socket Unix do autorizador")
	keyFile := flag.String("key-file", "/etc/shinyd/gateway.key", "Chave compartilhada do serviço")
	panel := flag.String("panel-origin", "", "Origem HTTPS do painel")
	dev := flag.Bool("development-http", false, "Somente experimentos HTTP em localhost")
	flag.Parse()
	host, _, err := net.SplitHostPort(*bind)
	ip := net.ParseIP(host)
	if err != nil || ip == nil || !ip.IsLoopback() || flag.NArg() != 0 {
		return fmt.Errorf("listener inválido; use endereço IP de loopback")
	}
	var listener net.Listener
	if *activated {
		listener, err = agent.ActivatedListener("/run/shinyd-gateway/http.sock")
	} else {
		listener, err = net.Listen("tcp", *bind)
	}
	if err != nil {
		return fmt.Errorf("falha ao obter listener do gateway")
	}
	defer listener.Close()
	info, err := os.Lstat(*keyFile)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0027 != 0 {
		return fmt.Errorf("chave exige arquivo regular privado (0600 ou 0640)")
	}
	key, err := os.ReadFile(*keyFile)
	if err != nil {
		return fmt.Errorf("não foi possível ler a chave do gateway")
	}
	control, err := gateway.NewUnixControl(*socket, strings.TrimSpace(string(key)))
	if err != nil {
		return err
	}
	scheme := "https"
	if *dev {
		scheme = "http"
	}
	handler, err := gateway.NewManaged(control, *panel, scheme, 20*time.Second)
	if err != nil {
		return err
	}
	server := &http.Server{
		Addr: *bind, Handler: handler, ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout: time.Minute, MaxHeaderBytes: 32 * 1024,
		ErrorLog: log.New(io.Discard, "", 0),
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		// Shutdown does not close hijacked WebSockets. Process exit closes them;
		// installing/updating the gateway currently requires a maintenance window.
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	if err := server.Serve(listener); err != nil && err != http.ErrServerClosed {
		return fmt.Errorf("falha ao iniciar listener do gateway")
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
