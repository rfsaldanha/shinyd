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
	"strconv"
	"syscall"
	"time"

	"github.com/rfsaldanha/shinyd/internal/agent"
	"github.com/rfsaldanha/shinyd/internal/executor"
)

func run() error {
	config := flag.String("config", "/etc/shinyd-runtime/policy.json", "Catálogo local aprovado pelo operador")
	flag.Parse()
	if flag.NArg() != 0 || os.Geteuid() != 0 {
		return errors.New("o executor exige o serviço root configurado")
	}
	policy, err := executor.LoadPolicy(*config)
	if err != nil {
		return errors.New("política do executor inválida")
	}
	account, err := user.Lookup("shinyd-control")
	if err != nil {
		return err
	}
	uid, err := strconv.ParseUint(account.Uid, 10, 32)
	if err != nil || uid == 0 {
		return errors.New("identidade de controle inválida")
	}
	group, err := user.LookupGroup("shinyd-gateway")
	if err != nil {
		return err
	}
	gid, err := strconv.Atoi(group.Gid)
	if err != nil || gid == 0 {
		return errors.New("grupo do gateway inválido")
	}
	for _, path := range []string{"/var/lib/shinyd-runtime", "/run/shinyd-runtime/apps"} {
		if err = executor.SecureDir(path, 0); err != nil {
			return errors.New("diretórios do executor inválidos")
		}
	}
	listener, err := agent.ActivatedListener("/run/shinyd-runtime/control.sock")
	if err != nil {
		return err
	}
	defer listener.Close()
	store, err := executor.OpenStore("/var/lib/shinyd-runtime", 0)
	if err != nil {
		return errors.New("estado indisponível ou em uso")
	}
	defer store.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	if policy.MemoryBudgetMiB == 0 {
		total, _, err := executor.Memory()
		if err != nil {
			return err
		}
		reserve := max(uint64(1<<30), total*15/100)
		if total <= reserve {
			return executor.ErrAdmission
		}
		policy.MemoryBudgetMiB = int((total - reserve) >> 20)
	}
	if err = executor.ConfigureBudget(ctx, executor.RunCommand, policy); err != nil {
		return fmt.Errorf("pré-requisitos do executor indisponíveis: %s", err)
	}
	manager, err := executor.NewManager(policy, store, executor.Podman{Run: executor.RunCommand, RunEnvironment: executor.RunWithEnvironment},
		executor.RealAttachment("/run/shinyd-runtime/apps", gid), executor.AdmissionAvailable,
		executor.BuilderArtifactResolver(executor.RunCommand))
	if err != nil {
		return errors.New("estado persistente inválido")
	}
	defer manager.Close()
	// Recovery precedes serving requests; it only reconnects existing workloads.
	manager.Recover(ctx)
	logger := log.New(os.Stdout, "", 0)
	server := executor.NewServer(uint32(uid), manager, func(event executor.Event) { data, _ := json.Marshal(event); logger.Print(string(data)) })
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				manager.Observe(ctx)
			}
		}
	}()
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		<-ctx.Done()
		deadline, cancel := context.WithTimeout(context.Background(), 65*time.Second)
		defer cancel()
		if server.Shutdown(deadline) != nil {
			server.Close()
		}
	}()
	err = server.Serve(agent.LimitListener(listener, 32))
	stop()
	<-stopped
	if err != nil && err != http.ErrServerClosed {
		return errors.New("falha no socket do executor")
	}
	return nil
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
