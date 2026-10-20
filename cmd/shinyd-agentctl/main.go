package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/rfsaldanha/shinyd/internal/agent"
)

func main() {
	socket := flag.String("socket", "/run/shinyd-agent/control.sock", "Socket Unix do agente")
	uid := flag.Uint("expected-server-uid", 0, "UID do dono do listener; 0 no serviço de sistema")
	flag.Parse()
	if flag.NArg() != 1 || flag.Arg(0) != "status" || uint64(*uid) > 1<<32-1 {
		fmt.Fprintln(os.Stderr, "Uso: shinyd-agentctl [--socket CAMINHO] status")
		os.Exit(2)
	}
	status, err := agent.ReadStatus(context.Background(), *socket, uint32(*uid))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if json.NewEncoder(os.Stdout).Encode(status) != nil {
		os.Exit(1)
	}
}
