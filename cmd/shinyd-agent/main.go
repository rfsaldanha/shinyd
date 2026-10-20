// shinyd-agent is a Linux-only service activated by a private systemd socket.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/rfsaldanha/shinyd/internal/agent"
	"github.com/rfsaldanha/shinyd/internal/doctor"
)

func run() error {
	caller := flag.String("allowed-user", "shinyd-control", "Único usuário Linux autorizado")
	socket := flag.String("socket-path", "/run/shinyd-agent/control.sock", "Socket herdado do systemd")
	data := flag.String("data-dir", "/var/lib/shinyd", "Diretório fixo observado no diagnóstico")
	flag.Parse()
	if flag.NArg() != 0 || !filepath.IsAbs(*data) || !filepath.IsAbs(*socket) {
		return errors.New("configuração do agente inválida")
	}
	account, err := user.Lookup(*caller)
	if err != nil {
		return errors.New("usuário de controle não encontrado")
	}
	uid, err := strconv.ParseUint(account.Uid, 10, 32)
	if err != nil || uid == 0 {
		return errors.New("o usuário de controle deve ser uma conta sem privilégios")
	}
	listener, err := agent.ActivatedListener(*socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	logger := log.New(os.Stdout, "", 0)
	server := agent.NewServer(uint32(uid), func() doctor.Report {
		return doctor.Inspect(doctor.Local(), *data)
	}, func(event agent.Audit) {
		data, _ := json.Marshal(event)
		logger.Print(string(data))
	})
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		<-ctx.Done()
		deadline, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		if server.Shutdown(deadline) != nil {
			_ = server.Close()
		}
	}()
	err = server.Serve(agent.LimitListener(listener, 32))
	stop()
	<-stopped
	if err != nil && err != http.ErrServerClosed {
		return errors.New("falha no serviço do agente")
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
