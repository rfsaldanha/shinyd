package gateway

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestDrainDenialNeverReachesApplication(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("draining request reached app")
	}))
	defer upstream.Close()
	g, _ := NewManaged(controlStub{track: func(_ context.Context, a RequestActivity) (Decision, error) {
		if a.Operation == "close" {
			return Decision{Allowed: true}, nil
		}
		if a.Kind != "websocket" || !instanceID.MatchString(a.RequestID) {
			t.Error("invalid reservation")
		}
		return Decision{Reason: "draining"}, nil
	}}, "http://panel.localhost", "http", time.Second)
	r := httptest.NewRequest("GET", "http://app.localhost/websocket/", nil)
	r.Header.Set("Upgrade", "websocket")
	r.Header.Set("Origin", "http://app.localhost")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 503 || w.Header().Get("Retry-After") == "" {
		t.Fatal("missing drain explanation")
	}
}

func TestWebSocketReservationCoversUpgradeUntilConnectionCloses(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, rw, _ := w.(http.Hijacker).Hijack()
		defer conn.Close()
		fmt.Fprint(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
		rw.Flush()
		io.Copy(conn, conn)
	}))
	defer upstream.Close()
	var mu sync.Mutex
	var steps []string
	var id string
	closed := make(chan struct{})
	g, _ := NewManaged(controlStub{track: func(_ context.Context, a RequestActivity) (Decision, error) {
		mu.Lock()
		defer mu.Unlock()
		if id == "" {
			id = a.RequestID
		}
		if id != a.RequestID || a.Kind != "websocket" {
			t.Error("connection identity changed")
		}
		steps = append(steps, a.Operation)
		if a.Operation == "close" {
			close(closed)
		}
		return Decision{Allowed: true, InstanceID: testInstance, Target: upstream.URL}, nil
	}}, "http://panel.localhost", "http", time.Second)
	server := httptest.NewServer(g)
	defer server.Close()
	conn, err := net.Dial("tcp", strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(2 * time.Second))
	fmt.Fprint(conn, "GET /websocket/ HTTP/1.1\r\nHost: app.localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nOrigin: http://app.localhost\r\n\r\n")
	reader := bufio.NewReader(conn)
	res, err := http.ReadResponse(reader, &http.Request{Method: "GET"})
	if err != nil || res.StatusCode != 101 {
		t.Fatal("upgrade failed", err)
	}
	fmt.Fprint(conn, "X")
	if b, err := reader.ReadByte(); err != nil || b != 'X' {
		t.Fatal("connection not preserved", err)
	}
	select {
	case <-closed:
		t.Fatal("reservation released during session")
	default:
	}
	conn.Close()
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("reservation not released")
	}
	mu.Lock()
	defer mu.Unlock()
	if steps[0] != "open" || steps[1] != "upgrade" || steps[len(steps)-1] != "close" {
		t.Fatal(steps)
	}
}

func TestHTTPStreamRenewsAndClosesOnTelemetryFailure(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer upstream.Close()
	closed := make(chan struct{})
	var pulses atomic.Int32
	g, _ := NewManaged(controlStub{track: func(_ context.Context, a RequestActivity) (Decision, error) {
		if a.Kind != "http" {
			t.Error("wrong stream classification")
		}
		if a.Operation == "pulse" {
			pulses.Add(1)
			return Decision{}, errors.New("unavailable")
		}
		if a.Operation == "close" {
			close(closed)
		}
		return Decision{Allowed: true, InstanceID: testInstance, Target: upstream.URL}, nil
	}}, "http://panel.localhost", "http", 10*time.Millisecond)
	server := httptest.NewServer(g)
	defer server.Close()
	client := &http.Client{Timeout: 2 * time.Second}
	req, _ := http.NewRequest("GET", server.URL, nil)
	req.Host = "app.localhost"
	res, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	io.ReadAll(res.Body)
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("stream did not close")
	}
	if pulses.Load() != 1 {
		t.Fatal("long HTTP request was not renewed")
	}
}
