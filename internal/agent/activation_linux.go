package agent

import (
	"errors"
	"net"
	"os"
	"strconv"
)

// ActivatedListener accepts exactly the descriptor handed over by systemd.
// It never binds a TCP port, replaces a socket file or unlinks an inherited one.
func ActivatedListener(socketPath string) (net.Listener, error) {
	pid, err := strconv.Atoi(os.Getenv("LISTEN_PID"))
	if err != nil || pid != os.Getpid() || os.Getenv("LISTEN_FDS") != "1" ||
		os.Getenv("LISTEN_FDNAMES") != "control" {
		return nil, errors.New("inicie o agente pelo socket systemd")
	}
	for _, name := range []string{"LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"} {
		_ = os.Unsetenv(name)
	}
	file := os.NewFile(3, "systemd-control")
	if file == nil {
		return nil, errors.New("descritor do socket ausente")
	}
	listener, err := net.FileListener(file)
	_ = file.Close()
	if err != nil {
		return nil, errors.New("descritor do socket inválido")
	}
	unix, ok := listener.(*net.UnixListener)
	if !ok || unix.Addr().String() != socketPath {
		_ = listener.Close()
		return nil, errors.New("o agente exige seu socket Unix configurado")
	}
	unix.SetUnlinkOnClose(false)
	return unix, nil
}
