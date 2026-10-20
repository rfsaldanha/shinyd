// Package gateway implements the HTTP/WebSocket data path. Registration,
// admission and authentication policy belong to the control plane.
package gateway

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"
)

type Route struct {
	Host       string
	Target     *url.URL
	InstanceID string
	Public     bool
}

type Authorizer func(context.Context, *http.Request, Route) error

type Gateway struct {
	routes          map[string]Route
	authorize       Authorizer
	scheme          string
	recheckInterval time.Duration
}

func New(routes []Route, authorize Authorizer, scheme string, interval time.Duration) (*Gateway, error) {
	if scheme != "http" && scheme != "https" {
		return nil, errors.New("invalid external scheme")
	}
	if interval <= 0 || interval > 30*time.Second {
		return nil, errors.New("authorization recheck must be between 0 and 30 seconds")
	}
	g := &Gateway{make(map[string]Route), authorize, scheme, interval}
	for _, route := range routes {
		if route.Target == nil || route.Target.Scheme != "http" ||
			route.Target.User != nil || route.Target.RawQuery != "" ||
			(route.Target.Path != "" && route.Target.Path != "/") ||
			route.Target.Fragment != "" {
			return nil, errors.New("invalid target")
		}
		ip := net.ParseIP(route.Target.Hostname())
		if ip == nil || !ip.IsLoopback() || route.Target.Port() == "" {
			return nil, errors.New("target must be an explicit loopback address and port")
		}
		if route.Host == "" || strings.ContainsAny(route.Host, "/\\ \r\n") ||
			route.InstanceID == "" {
			return nil, errors.New("invalid route")
		}
		host := strings.ToLower(route.Host)
		if _, exists := g.routes[host]; exists {
			return nil, errors.New("duplicate host")
		}
		// Copy mutable URL values so callers cannot change validated targets.
		target := *route.Target
		route.Target = &target
		g.routes[host] = route
	}
	return g, nil
}

func (g *Gateway) allowed(ctx context.Context, r *http.Request, route Route) bool {
	if route.Public {
		return true
	}
	if g.authorize == nil {
		return false
	}
	// Authorizers must honor context cancellation (e.g. HTTP over a Unix socket).
	checkContext, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	return g.authorize(checkContext, r, route) == nil && checkContext.Err() == nil
}

func reservedCookie(name string) bool {
	return strings.HasPrefix(name, "__Host-shinyd") ||
		strings.HasPrefix(name, "shinyd_")
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	route, exists := g.routes[strings.ToLower(r.Host)]
	if !exists {
		http.NotFound(w, r)
		return
	}
	if !g.allowed(r.Context(), r, route) {
		http.Error(w, "Acesso não autorizado.", http.StatusForbidden)
		return
	}
	websocket := strings.EqualFold(r.Header.Get("Upgrade"), "websocket")
	origin := r.Header.Get("Origin")
	if (websocket && origin == "") ||
		(origin != "" && origin != g.scheme+"://"+r.Host) {
		http.Error(w, "Origem não autorizada.", http.StatusForbidden)
		return
	}
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	done := make(chan struct{})
	defer close(done)
	if websocket && !route.Public {
		// Request metadata is read-only. Never mutate the authorization copy.
		authRequest := r.Clone(ctx)
		go func() {
			ticker := time.NewTicker(g.recheckInterval)
			defer ticker.Stop()
			for {
				select {
				case <-done:
					return
				case <-ctx.Done():
					return
				case <-ticker.C:
					if !g.allowed(ctx, authRequest, route) {
						cancel()
						return
					}
				}
			}
		}()
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(p *httputil.ProxyRequest) {
			p.SetURL(route.Target)
			p.Out.Host = p.In.Host
			// Identity must never come from client-supplied headers.
			for key := range p.Out.Header {
				if strings.HasPrefix(strings.ToLower(key), "x-shinyd-") ||
					strings.HasPrefix(strings.ToLower(key), "x-forwarded-") ||
					strings.EqualFold(key, "Forwarded") ||
					strings.EqualFold(key, "Authorization") {
					p.Out.Header.Del(key)
				}
			}
			p.SetXForwarded()
			p.Out.Header.Set("X-Forwarded-Proto", g.scheme)
			p.Out.Header.Del("Cookie")
			for _, cookie := range p.In.Cookies() {
				if !reservedCookie(cookie.Name) {
					p.Out.AddCookie(cookie)
				}
			}
		},
		ModifyResponse: func(response *http.Response) error {
			cookies := response.Cookies()
			response.Header.Del("Set-Cookie")
			for _, cookie := range cookies {
				if reservedCookie(cookie.Name) {
					continue
				}
				cookie.Domain = "" // Apps cannot plant sibling or panel cookies.
				if g.scheme == "https" {
					cookie.Secure = true
				}
				response.Header.Add("Set-Cookie", cookie.String())
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "A instância está indisponível.", http.StatusBadGateway)
		},
		// Default proxy errors may contain a credential-bearing URL. The control
		// plane records structured instance failures instead.
		ErrorLog: log.New(io.Discard, "", 0),
	}
	proxy.ServeHTTP(w, r.WithContext(ctx))
}
