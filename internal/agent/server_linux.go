// Package agent defines the private, versioned executor boundary. The first
// capability is read-only diagnostics; there is deliberately no command runner.
package agent

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"sync"
	"syscall"
	"time"

	"github.com/rfsaldanha/shinyd/internal/doctor"
)

const StatusPath = "/v1/status"

type Status struct {
	SchemaVersion  int           `json:"schema_version"`
	Mode           string        `json:"mode"`
	Capabilities   []string      `json:"capabilities"`
	RuntimeEnabled bool          `json:"runtime_enabled"`
	Report         doctor.Report `json:"report"`
}

type Audit struct {
	Event     string `json:"event"`
	Operation string `json:"operation"`
	CallerUID uint32 `json:"caller_uid"`
	Result    int    `json:"result"`
}

type peerKey struct{}

type peer struct {
	UID   uint32
	Valid bool
}

// PeerUID uses kernel credentials, never a request header or body field.
func PeerUID(conn net.Conn) (uint32, error) {
	if limited, ok := conn.(*limitedConn); ok {
		conn = limited.Conn
	}
	unix, ok := conn.(*net.UnixConn)
	if !ok {
		return 0, errors.New("Unix connection required")
	}
	raw, err := unix.SyscallConn()
	if err != nil {
		return 0, err
	}
	var credentials *syscall.Ucred
	var socketErr error
	if err = raw.Control(func(fd uintptr) {
		credentials, socketErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	}); err != nil {
		return 0, err
	}
	if socketErr != nil || credentials == nil {
		return 0, errors.New("peer credentials unavailable")
	}
	return credentials.Uid, nil
}

// NewServer accepts exactly one OS identity. Root is not an implicit bypass.
// observe and audit are trusted dependencies, never supplied by the caller.
func NewServer(allowedUID uint32, observe func() doctor.Report, audit func(Audit)) *http.Server {
	var mu sync.Mutex
	var cached Status
	var observed time.Time
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		// Short connections also bound the lifetime of authenticated peer identity.
		w.Header().Set("Connection", "close")
		caller, ok := r.Context().Value(peerKey{}).(peer)
		code := http.StatusForbidden
		operation := "rejected"
		defer func() {
			if audit != nil {
				// No paths, query strings, headers, bodies or diagnostic output.
				audit(Audit{"agent.request", operation, caller.UID, code})
			}
		}()
		fail := func(status int, reason string) {
			code = status
			w.WriteHeader(status)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": reason})
		}
		if !ok || !caller.Valid || caller.UID != allowedUID {
			fail(http.StatusForbidden, "caller_denied")
			return
		}
		if r.URL.Path != StatusPath || r.URL.RawPath != "" {
			fail(http.StatusNotFound, "operation_unknown")
			return
		}
		operation = "status"
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			fail(http.StatusMethodNotAllowed, "method_denied")
			return
		}
		if r.Host != "agent" || r.URL.IsAbs() || r.URL.ForceQuery || r.URL.RawQuery != "" ||
			r.ContentLength != 0 || len(r.TransferEncoding) != 0 || r.Header.Get("Upgrade") != "" {
			fail(http.StatusBadRequest, "arguments_denied")
			return
		}
		mu.Lock()
		if observed.IsZero() || time.Since(observed) >= 10*time.Second {
			cached = Status{1, "diagnostic", []string{"status"}, false, observe()}
			observed = time.Now()
		}
		snapshot := cached
		mu.Unlock()
		code = http.StatusOK
		_ = json.NewEncoder(w).Encode(snapshot)
	})
	server := &http.Server{
		Handler: handler, ReadHeaderTimeout: 2 * time.Second,
		ReadTimeout: 3 * time.Second, WriteTimeout: 3 * time.Second,
		IdleTimeout: 2 * time.Second, MaxHeaderBytes: 4096,
		ErrorLog: log.New(io.Discard, "", 0),
		ConnContext: func(ctx context.Context, conn net.Conn) context.Context {
			uid, err := PeerUID(conn)
			return context.WithValue(ctx, peerKey{}, peer{uid, err == nil})
		},
	}
	server.SetKeepAlivesEnabled(false)
	return server
}

// LimitListener bounds concurrent connections, including clients which never
// send HTTP headers. ReadHeaderTimeout releases those slots.
func LimitListener(listener net.Listener, limit int) net.Listener {
	return &limitedListener{Listener: listener, slots: make(chan struct{}, limit), done: make(chan struct{})}
}

type limitedListener struct {
	net.Listener
	slots chan struct{}
	done  chan struct{}
	once  sync.Once
}

func (l *limitedListener) Accept() (net.Conn, error) {
	select {
	case l.slots <- struct{}{}:
	case <-l.done:
		return nil, net.ErrClosed
	}
	conn, err := l.Listener.Accept()
	if err != nil {
		<-l.slots
		return nil, err
	}
	// PeerUID unwraps only this private wrapper before obtaining credentials.
	return &limitedConn{Conn: conn, release: func() { <-l.slots }}, nil
}

func (l *limitedListener) Close() error {
	l.once.Do(func() { close(l.done) })
	return l.Listener.Close()
}

type limitedConn struct {
	net.Conn
	once    sync.Once
	release func()
}

func (c *limitedConn) Close() error {
	err := c.Conn.Close()
	c.once.Do(c.release)
	return err
}
