package gateway

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testInstance = "11111111-1111-4111-8111-111111111111"
const testToken = "0123456789012345678901234567890123456789012"

type controlStub struct {
	auth     func(context.Context, string, string) (Decision, error)
	exchange func(context.Context, string, string) (Credential, error)
}

func (s controlStub) Authorize(ctx context.Context, host, token string) (Decision, error) {
	return s.auth(ctx, host, token)
}
func (s controlStub) Exchange(ctx context.Context, host, code string) (Credential, error) {
	return s.exchange(ctx, host, code)
}

func TestManagedExchangeRequiresPanelOriginAndSetsHostCookie(t *testing.T) {
	calls := 0
	control := controlStub{exchange: func(_ context.Context, host, code string) (Credential, error) {
		calls++
		if host != "app.example" || code != testToken {
			t.Fatal("wrong handoff binding")
		}
		return Credential{testToken, 3600}, nil
	}}
	g, err := NewManaged(control, "https://panel.example", "https", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	for _, origin := range []string{"", "https://evil.example", "https://panel.example"} {
		r := httptest.NewRequest("POST", "https://app.example"+ConnectPath, strings.NewReader("code="+testToken))
		r.Header.Set("Origin", origin)
		r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
		if origin != "https://panel.example" {
			if w.Code != 403 {
				t.Fatal("bad origin accepted")
			}
			continue
		}
		if w.Code != 303 || w.Header().Get("Location") != "/" {
			t.Fatal("unsafe redirect")
		}
		cookies := w.Result().Cookies()
		if len(cookies) != 1 {
			t.Fatal("missing cookie")
		}
		c := cookies[0]
		if c.Name != "__Host-shinyd_app" || !c.Secure || !c.HttpOnly || c.Domain != "" ||
			c.Path != "/" || c.SameSite != http.SameSiteLaxMode {
			t.Fatal("unsafe cookie")
		}
		if strings.Contains(w.Body.String(), testToken) {
			t.Fatal("token in response body")
		}
	}
	if calls != 1 {
		t.Fatal("invalid request reached exchange")
	}
}

func TestManagedRejectsQueryReplayAndOversizedExchange(t *testing.T) {
	control := controlStub{exchange: func(context.Context, string, string) (Credential, error) {
		t.Fatal("invalid exchange reached control")
		return Credential{}, nil
	}}
	g, _ := NewManaged(control, "http://panel.localhost", "http", time.Second)
	for _, tc := range []struct{ path, body string }{
		{ConnectPath + "?code=" + testToken, "code=" + testToken},
		{ConnectPath, "code=" + testToken + "&code=" + testToken},
		{ConnectPath, "code=" + strings.Repeat("x", 2048)},
		{ConnectPath, "code=" + testToken + "&next=https://evil"},
	} {
		r := httptest.NewRequest("POST", "http://app.localhost"+tc.path, strings.NewReader(tc.body))
		r.Header.Set("Origin", "http://panel.localhost")
		r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
		if w.Code < 400 {
			t.Fatal("malformed exchange accepted")
		}
	}
}

func TestManagedDeniesDirectHTTPAndWebSocketAndControlOutage(t *testing.T) {
	var down atomic.Bool
	g, _ := NewManaged(controlStub{auth: func(context.Context, string, string) (Decision, error) {
		if down.Load() {
			return Decision{}, errors.New("outage")
		}
		return Decision{InstanceID: testInstance}, nil
	}}, "http://panel.localhost", "http", time.Second)
	for _, path := range []string{"/", "/download/secret", "/websocket/"} {
		r := httptest.NewRequest("GET", "http://app.localhost"+path, nil)
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
		if w.Code != 403 || !strings.Contains(w.Body.String(), "/connect/"+testInstance+"/") {
			t.Fatal("unauthorized access or missing login route")
		}
	}
	down.Store(true)
	w := httptest.NewRecorder()
	g.ServeHTTP(w, httptest.NewRequest("GET", "http://app.localhost/", nil))
	if w.Code != 503 {
		t.Fatal("outage failed open")
	}
}

func TestManagedAuthorizationBindsCredentialAndNeverForwardsIt(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Cookie") != "app_setting=ok" {
			t.Error("credential forwarded upstream")
		}
		w.Header().Set("Clear-Site-Data", `"cookies"`)
		w.Header().Set("Service-Worker-Allowed", "/")
		fmt.Fprint(w, "application")
	}))
	defer upstream.Close()
	g, _ := NewManaged(controlStub{auth: func(_ context.Context, host, token string) (Decision, error) {
		calls.Add(1)
		if host != "app.localhost" || token != testToken {
			t.Fatal("wrong auth context")
		}
		return Decision{Allowed: true, InstanceID: testInstance, Target: upstream.URL}, nil
	}}, "http://panel.localhost", "http", time.Second)
	r := httptest.NewRequest("GET", "http://app.localhost/", nil)
	r.Header.Set("Cookie", "shinyd_dev_app="+testToken+"; app_setting=ok; shinyd_dev_session=panel")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 200 || w.Body.String() != "application" || calls.Load() != 1 {
		t.Fatal("routing failed")
	}
	if w.Header().Get("Clear-Site-Data") != "" || w.Header().Get("Service-Worker-Allowed") != "" {
		t.Fatal("unsafe browser state headers")
	}
	r.Header.Set("Service-Worker", "script")
	w = httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("service worker registration accepted")
	}
}

func TestManagedPublicConnectionClosesWhenVisibilityChanges(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, rw, _ := w.(http.Hijacker).Hijack()
		defer conn.Close()
		fmt.Fprint(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
		rw.Flush()
		_, _ = conn.Read(make([]byte, 1))
	}))
	defer upstream.Close()
	var allowed atomic.Bool
	allowed.Store(true)
	g, _ := NewManaged(controlStub{auth: func(_ context.Context, _, token string) (Decision, error) {
		if token != "" {
			t.Error("unexpected credential")
		}
		return Decision{Allowed: allowed.Load(), InstanceID: testInstance, Target: upstream.URL}, nil
	}}, "http://panel.localhost", "http", 10*time.Millisecond)
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
	allowed.Store(false)
	if _, err := reader.ReadByte(); err == nil {
		t.Fatal("revoked connection stayed open")
	} else if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
		t.Fatal("connection timed out instead of closing")
	}
}

func TestUnixControlUsesOnlySocketAndDoesNotFollowRedirects(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "control.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	key := strings.Repeat("a", 64)
	var redirect atomic.Bool
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+key {
			t.Error("missing service credential")
		}
		if redirect.Load() {
			w.Header().Set("Location", "http://127.0.0.1:1/steal")
			w.WriteHeader(302)
			return
		}
		fmt.Fprintf(w, `{"allowed":true,"instance_id":%q,"target":"http://127.0.0.1:3838"}`, testInstance)
	})}
	go server.Serve(listener)
	defer server.Close()
	control, _ := NewUnixControl(socket, key)
	decision, err := control.Authorize(context.Background(), "app.localhost", testToken)
	if err != nil || !decision.Allowed {
		t.Fatal("unix auth failed", err)
	}
	redirect.Store(true)
	if _, err := control.Authorize(context.Background(), "app.localhost", testToken); err == nil {
		t.Fatal("control redirect accepted")
	}
	control.client.Transport.(*http.Transport).CloseIdleConnections()
	if _, err := NewManaged(control, "http://panel.example", "http", time.Second); err == nil {
		t.Fatal("nonlocal cleartext accepted")
	}
}
