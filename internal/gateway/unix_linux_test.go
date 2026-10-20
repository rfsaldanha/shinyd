package gateway

import (
	"context"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestUnixTargetBoundToInstance(t *testing.T) {
	id := "11111111-1111-4111-8111-111111111111"
	valid := "unix:/run/shinyd-runtime/apps/" + id + ".sock"
	for _, address := range []string{valid, "unix:/run/podman/podman.sock", "unix:/tmp/" + id + ".sock", valid + "?x=1", valid + "#x", valid + "?", "unix://host/run/shinyd-runtime/apps/" + id + ".sock"} {
		u, _ := url.Parse(address)
		_, err := New([]Route{{Host: "app.localhost", Target: u, InstanceID: id}}, nil, "http", time.Second)
		if (err == nil) != (address == valid) {
			t.Fatalf("%s: %v", address, err)
		}
	}
	u, _ := url.Parse(valid)
	if _, err := New([]Route{{Host: "app.localhost", Target: u, InstanceID: "22222222-2222-4222-8222-222222222222"}}, nil, "http", time.Second); err == nil {
		t.Fatal("cross-instance socket allowed")
	}
}
func TestUnixTransportRequiresPeerUID(t *testing.T) {
	path := filepath.Join(t.TempDir(), "socket")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(204) })}
	go server.Serve(listener)
	defer server.Close()
	for _, uid := range []uint32{uint32(os.Getuid()), uint32(os.Getuid() + 1)} {
		transport := unixTransport(path, uid)
		request, _ := http.NewRequestWithContext(context.Background(), "GET", "http://runtime/", nil)
		response, err := (&http.Client{Transport: transport, Timeout: time.Second}).Do(request)
		if uid == uint32(os.Getuid()) {
			if err != nil || response.StatusCode != 204 {
				t.Fatal(err)
			}
			response.Body.Close()
		} else if err == nil {
			response.Body.Close()
			t.Fatal("accepted untrusted peer")
		}
		transport.CloseIdleConnections()
	}
}
