package gateway

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func target(raw string) *url.URL {
	u, _ := url.Parse(raw)
	return u
}

func TestPrivateRequestsAndWebSocketsFailClosed(t *testing.T) {
	var hits atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
	}))
	defer upstream.Close()
	g, err := New([]Route{{"a.localhost", target(upstream.URL), "a", false}}, nil, "http", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/", "/session/upload", "/download", "/websocket/"} {
		r := httptest.NewRequest("GET", "http://a.localhost"+path, nil)
		if strings.Contains(path, "websocket") {
			r.Header.Set("Upgrade", "websocket")
			r.Header.Set("Connection", "Upgrade")
		}
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
		if w.Code != 403 {
			t.Fatalf("%s: %d", path, w.Code)
		}
	}
	if hits.Load() != 0 {
		t.Fatal("unauthorized request reached upstream")
	}
}

func TestHostAffinityAndHeaderBoundary(t *testing.T) {
	upstream := func(id string) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "" || r.Header.Get("X-Shinyd-User") != "" {
				t.Error("untrusted identity forwarded")
			}
			if _, err := r.Cookie("__Host-shinyd_app"); err == nil {
				t.Error("gateway credential leaked to app")
			}
			http.SetCookie(w, &http.Cookie{Name: "__Host-shinyd_app", Value: "forged", Path: "/"})
			http.SetCookie(w, &http.Cookie{Name: "app_cookie", Value: id, Domain: "localhost"})
			fmt.Fprint(w, id)
		}))
	}
	a, b := upstream("a"), upstream("b")
	defer a.Close()
	defer b.Close()
	g, err := New([]Route{
		{"a.localhost", target(a.URL), "a", true},
		{"b.localhost", target(b.URL), "b", true},
	}, nil, "http", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	for _, host := range []string{"a.localhost", "b.localhost", "a.localhost"} {
		r := httptest.NewRequest("GET", "http://"+host+"/", nil)
		r.Header.Set("X-Shinyd-User", "admin")
		r.Header.Set("Authorization", "Bearer admin-token")
		r.AddCookie(&http.Cookie{Name: "__Host-shinyd_app", Value: "private"})
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
		if w.Body.String() != host[:1] {
			t.Fatal("request lost host affinity")
		}
		cookies := w.Result().Cookies()
		if len(cookies) != 1 || cookies[0].Name != "app_cookie" || cookies[0].Domain != "" {
			t.Fatal("unsafe upstream cookies")
		}
	}
}

func TestBadOriginsAndTargets(t *testing.T) {
	for _, raw := range []string{"http://169.254.169.254:80", "http://example.com:80", "file:///etc/passwd"} {
		if _, err := New([]Route{{"a", target(raw), "a", true}}, nil, "http", time.Second); err == nil {
			t.Fatal("unsafe target accepted")
		}
	}
	g, _ := New([]Route{{"a", target("http://127.0.0.1:1"), "a", true}}, nil, "http", time.Second)
	r := httptest.NewRequest("GET", "http://a/websocket/", nil)
	r.Header.Set("Upgrade", "websocket")
	r.Header.Set("Origin", "http://evil")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("cross-origin websocket accepted")
	}
}

func TestRevocationClosesAnAlreadyUpgradedConnection(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, rw, _ := w.(http.Hijacker).Hijack()
		defer conn.Close()
		fmt.Fprint(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
		rw.Flush()
		buffer := make([]byte, 1)
		conn.Read(buffer)
	}))
	defer upstream.Close()
	var allowed atomic.Bool
	allowed.Store(true)
	auth := func(context.Context, *http.Request, Route) error {
		if !allowed.Load() {
			return errors.New("revoked")
		}
		return nil
	}
	g, _ := New([]Route{{"a.localhost", target(upstream.URL), "a", false}}, auth, "http", 10*time.Millisecond)
	server := httptest.NewServer(g)
	defer server.Close()
	conn, err := net.Dial("tcp", strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(2 * time.Second))
	fmt.Fprint(conn, "GET /websocket/ HTTP/1.1\r\nHost: a.localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nOrigin: http://a.localhost\r\n\r\n")
	reader := bufio.NewReader(conn)
	response, err := http.ReadResponse(reader, &http.Request{Method: "GET"})
	if err != nil || response.StatusCode != 101 {
		t.Fatalf("upgrade failed: %v", err)
	}
	allowed.Store(false)
	_, err = reader.ReadByte()
	if err == nil {
		t.Fatal("revoked connection remained open")
	}
	if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
		t.Fatal("revoked connection timed out instead of closing")
	}
}
