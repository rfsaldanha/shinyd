package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"html/template"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync/atomic"
	"time"
)

const ConnectPath = "/_shinyd/connect"

var (
	opaqueToken = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)
	instanceID  = regexp.MustCompile(`^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$`)
)

type Decision struct {
	Allowed    bool   `json:"allowed"`
	InstanceID string `json:"instance_id"`
	Target     string `json:"target"`
	Reason     string `json:"reason"`
}

type Credential struct {
	Value  string `json:"credential"`
	MaxAge int    `json:"max_age"`
}

type Control interface {
	Authorize(context.Context, string, string) (Decision, error)
	Exchange(context.Context, string, string) (Credential, error)
}

// UnixControl has no TCP fallback, redirect support or ambient proxy settings.
type UnixControl struct {
	client *http.Client
	key    string
}

func NewUnixControl(socket, key string) (*UnixControl, error) {
	if !strings.HasPrefix(socket, "/") || !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(key) {
		return nil, errors.New("invalid control configuration")
	}
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
		MaxIdleConns: 32, MaxIdleConnsPerHost: 32, MaxConnsPerHost: 64,
		IdleConnTimeout: 30 * time.Second, ResponseHeaderTimeout: 2 * time.Second,
	}
	return &UnixControl{
		client: &http.Client{
			Transport: transport, Timeout: 2 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		key: key,
	}, nil
}

func (c *UnixControl) call(ctx context.Context, path string, input, output any) error {
	body, err := json.Marshal(input)
	if err != nil {
		return errors.New("control request failed")
	}
	request, err := http.NewRequestWithContext(ctx, "POST", "http://control"+path, bytes.NewReader(body))
	if err != nil {
		return errors.New("control request failed")
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+c.key)
	response, err := c.client.Do(request)
	if err != nil {
		return errors.New("control unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return errors.New("control denied request")
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 4097))
	decoder.DisallowUnknownFields()
	if decoder.Decode(output) != nil || decoder.Decode(new(any)) != io.EOF {
		return errors.New("invalid control response")
	}
	return nil
}

func (c *UnixControl) Authorize(ctx context.Context, host, credential string) (Decision, error) {
	var result Decision
	err := c.call(ctx, "/v1/authorize", map[string]string{"host": host, "credential": credential}, &result)
	return result, err
}

func (c *UnixControl) Exchange(ctx context.Context, host, code string) (Credential, error) {
	var result Credential
	err := c.call(ctx, "/v1/exchange", map[string]string{"host": host, "code": code}, &result)
	return result, err
}

type Managed struct {
	control     Control
	panelOrigin string
	scheme      string
	cookieName  string
	interval    time.Duration
}

func NewManaged(control Control, panelOrigin, scheme string, interval time.Duration) (*Managed, error) {
	panel, err := url.Parse(panelOrigin)
	if err != nil || panel.Scheme != scheme || panel.Host == "" || panel.User != nil ||
		panel.Path != "" || panel.RawQuery != "" || panel.Fragment != "" || control == nil ||
		(scheme != "http" && scheme != "https") || interval <= 0 || interval > 28*time.Second {
		return nil, errors.New("invalid managed gateway configuration")
	}
	if scheme == "http" && panel.Hostname() != "localhost" && !strings.HasSuffix(panel.Hostname(), ".localhost") {
		return nil, errors.New("development HTTP requires localhost")
	}
	cookie := "__Host-shinyd_app"
	if scheme == "http" {
		cookie = "shinyd_dev_app"
	}
	return &Managed{control, panelOrigin, scheme, cookie, interval}, nil
}

var deniedPage = template.Must(template.New("denied").Parse(`<!doctype html>
<html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Acesso restrito · shinyd</title><main><h1>Acesso restrito</h1>
<p>Entre pelo painel para abrir este aplicativo.</p><a href="{{.}}">Entrar</a></main></html>`))

func (g *Managed) denied(w http.ResponseWriter, id string) {
	link := g.panelOrigin + "/"
	if instanceID.MatchString(id) {
		link += "connect/" + id + "/"
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; base-uri 'none'; frame-ancestors 'none'")
	w.WriteHeader(http.StatusForbidden)
	_ = deniedPage.Execute(w, link)
}

func (g *Managed) exchange(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
	if r.Method != "POST" {
		w.Header().Set("Allow", "POST")
		http.Error(w, "Método não permitido.", http.StatusMethodNotAllowed)
		return
	}
	if r.Header.Get("Origin") != g.panelOrigin || r.URL.RawQuery != "" ||
		r.Header.Get("Content-Type") != "application/x-www-form-urlencoded" {
		http.Error(w, "Conexão não autorizada.", http.StatusForbidden)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 1024)
	if r.ParseForm() != nil || len(r.PostForm) != 1 || len(r.PostForm["code"]) != 1 ||
		!opaqueToken.MatchString(r.PostForm.Get("code")) {
		http.Error(w, "Código inválido.", http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	credential, err := g.control.Exchange(ctx, strings.ToLower(r.Host), r.PostForm.Get("code"))
	if err != nil || !opaqueToken.MatchString(credential.Value) || credential.MaxAge < 1 || credential.MaxAge > 3600 {
		http.Error(w, "Conexão expirada ou indisponível. Entre novamente pelo painel.", http.StatusForbidden)
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name: g.cookieName, Value: credential.Value, Path: "/",
		Secure: g.scheme == "https", HttpOnly: true, SameSite: http.SameSiteLaxMode,
		MaxAge: credential.MaxAge,
	})
	// No token, app-supplied redirect or original query survives the handoff.
	w.Header().Set("Location", "/")
	w.WriteHeader(http.StatusSeeOther)
}

func (g *Managed) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Referrer-Policy", "strict-origin")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.URL.Path == ConnectPath {
		g.exchange(w, r)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/_shinyd/") {
		http.NotFound(w, r)
		return
	}
	credential := ""
	found := false
	for _, cookie := range r.Cookies() {
		if cookie.Name == g.cookieName {
			if found || !opaqueToken.MatchString(cookie.Value) {
				g.denied(w, "")
				return
			}
			credential, found = cookie.Value, true
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	decision, err := g.control.Authorize(ctx, strings.ToLower(r.Host), credential)
	cancel()
	if err != nil {
		http.Error(w, "Controle de acesso indisponível.", http.StatusServiceUnavailable)
		return
	}
	if !decision.Allowed {
		if decision.Reason == "not_found" {
			http.NotFound(w, r)
		} else {
			g.denied(w, decision.InstanceID)
		}
		return
	}
	target, err := url.Parse(decision.Target)
	if err != nil || !instanceID.MatchString(decision.InstanceID) {
		http.Error(w, "Destino indisponível.", http.StatusServiceUnavailable)
		return
	}
	var initial atomic.Bool
	initial.Store(true)
	auth := func(ctx context.Context, request *http.Request, route Route) error {
		if initial.Swap(false) {
			return nil // The check immediately above already authorized this request.
		}
		next, err := g.control.Authorize(ctx, strings.ToLower(request.Host), credential)
		if err != nil || !next.Allowed || next.InstanceID != route.InstanceID || next.Target != decision.Target {
			return errors.New("access revoked or route changed")
		}
		return nil
	}
	// Public apps also recheck authorization: visibility can change during a WS.
	proxy, err := New([]Route{{r.Host, target, decision.InstanceID, false}}, auth, g.scheme, g.interval)
	if err != nil {
		http.Error(w, "Destino indisponível.", http.StatusServiceUnavailable)
		return
	}
	proxy.ServeHTTP(w, r)
}
