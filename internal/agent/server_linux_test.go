package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rfsaldanha/shinyd/internal/doctor"
)

func report() doctor.Report {
	return doctor.Report{SchemaVersion: 1, ObservedAt: time.Now().UTC(), OS: "linux", Checks: []doctor.Check{}}
}

func serve(t *testing.T, server *http.Server) (string, *http.Client) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "control.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(LimitListener(listener, 4)) }()
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", path)
	}}
	t.Cleanup(func() {
		transport.CloseIdleConnections()
		_ = server.Close()
	})
	return path, &http.Client{Transport: transport, Timeout: 4 * time.Second}
}

func TestKernelIdentityAndAudit(t *testing.T) {
	for _, allowed := range []bool{true, false} {
		t.Run(fmt.Sprint(allowed), func(t *testing.T) {
			expected := uint32(os.Getuid())
			if !allowed {
				expected++
			}
			events := make(chan Audit, 1)
			path, client := serve(t, NewServer(expected, report, func(event Audit) { events <- event }))
			request, _ := http.NewRequest("GET", "http://agent/v1/status", nil)
			request.Header.Set("X-Shinyd-UID", fmt.Sprint(expected))
			request.Header.Set("Authorization", "Bearer must-not-be-logged")
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			data, _ := io.ReadAll(response.Body)
			response.Body.Close()
			want := 403
			if allowed {
				want = 200
			}
			if response.StatusCode != want {
				t.Fatalf("got %d: %s", response.StatusCode, data)
			}
			event := <-events
			encoded, _ := json.Marshal(event)
			if event.CallerUID != uint32(os.Getuid()) || event.Result != want || bytes.Contains(encoded, []byte("must-not")) {
				t.Fatalf("audit: %s", encoded)
			}
			_ = path
		})
	}
}

func TestRejectsArgumentsAndUnknownOperations(t *testing.T) {
	cases := []struct {
		method, target, body string
		status               int
	}{
		{"GET", "/v1/status", "", 200},
		{"GET", "/v1/status?path=/etc/shadow", "", 400},
		{"GET", "/v1/status?", "", 400},
		{"GET", "/v1/status", `{"command":"id"}`, 400},
		{"POST", "/v1/status", `{}`, 405},
		{"GET", "/v1/%73tatus", "", 404},
		{"POST", "/v1/instances/start", `{"image":"untrusted"}`, 404},
		{"GET", "/v1/../status", "", 404},
		{"GET", "/v1/status/", "", 404},
	}
	_, client := serve(t, NewServer(uint32(os.Getuid()), report, nil))
	for _, c := range cases {
		t.Run(c.method+c.target+c.body, func(t *testing.T) {
			request, _ := http.NewRequest(c.method, "http://agent"+c.target, strings.NewReader(c.body))
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			if response.StatusCode != c.status {
				t.Fatalf("got %d want %d", response.StatusCode, c.status)
			}
		})
	}
}

func TestNoTCPOrUnverifiedContext(t *testing.T) {
	server := NewServer(0, report, nil)
	request := httptest.NewRequest("GET", "http://agent/v1/status", nil)
	recorder := httptest.NewRecorder()
	server.Handler.ServeHTTP(recorder, request)
	if recorder.Code != 403 {
		t.Fatal(recorder.Code)
	}
	tcp := httptest.NewServer(server.Handler)
	defer tcp.Close()
	response, err := tcp.Client().Get(tcp.URL + "/v1/status")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != 403 {
		t.Fatal(response.StatusCode)
	}
}

func TestClientVerifiesPeerAndFreshSchema(t *testing.T) {
	path, _ := serve(t, NewServer(uint32(os.Getuid()), report, nil))
	status, err := ReadStatus(context.Background(), path, uint32(os.Getuid()))
	if err != nil || status.RuntimeEnabled || status.Mode != "diagnostic" {
		t.Fatalf("%+v %v", status, err)
	}
	if _, err := ReadStatus(context.Background(), path, uint32(os.Getuid()+1)); err == nil {
		t.Fatal("accepted impersonating server")
	}
	for _, target := range []string{"http://localhost/", "relative", "/tmp/../tmp/a"} {
		if _, err := ReadStatus(context.Background(), target, 0); err == nil {
			t.Fatal("accepted invalid path")
		}
	}
	for _, payload := range []string{
		`{}`, strings.Repeat("x", 65537),
		`{"schema_version":1,"mode":"diagnostic","capabilities":["status"],"report":{"schema_version":1,"observed_at":"2000-01-01T00:00:00Z"}}`,
	} {
		path, _ := serve(t, &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			fmt.Fprint(w, payload)
		})})
		if _, err := ReadStatus(context.Background(), path, uint32(os.Getuid())); err == nil {
			t.Fatal("accepted invalid/stale payload")
		}
	}
}

func TestConcurrentStatusUsesBoundedSnapshot(t *testing.T) {
	var observations atomic.Int32
	path, _ := serve(t, NewServer(uint32(os.Getuid()), func() doctor.Report {
		observations.Add(1)
		return report()
	}, nil))
	var wg sync.WaitGroup
	for range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := ReadStatus(context.Background(), path, uint32(os.Getuid())); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if observations.Load() != 1 {
		t.Fatal(observations.Load())
	}
}

func TestClosedLimitedListenerDoesNotHang(t *testing.T) {
	listener, err := net.Listen("unix", filepath.Join(t.TempDir(), "socket"))
	if err != nil {
		t.Fatal(err)
	}
	limited := LimitListener(listener, 1)
	client, err := net.Dial("unix", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	conn, err := limited.Accept()
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	done := make(chan error, 1)
	go func() { _, err := limited.Accept(); done <- err }()
	limited.Close()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("closed listener accepted")
		}
	case <-time.After(time.Second):
		t.Fatal("blocked Accept after shutdown")
	}
}

func TestActivationRefusesMissingOrWrongDescriptors(t *testing.T) {
	for _, config := range []struct{ pid, count, name string }{
		{"", "", ""}, {"1", "1", "control"}, {fmt.Sprint(os.Getpid()), "2", "control"},
		{fmt.Sprint(os.Getpid()), "1", "other"},
	} {
		t.Setenv("LISTEN_PID", config.pid)
		t.Setenv("LISTEN_FDS", config.count)
		t.Setenv("LISTEN_FDNAMES", config.name)
		if listener, err := ActivatedListener("/run/shinyd-agent/control.sock"); err == nil {
			listener.Close()
			t.Fatal("invalid activation accepted")
		}
	}
}
