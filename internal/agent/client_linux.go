package agent

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"time"
)

// ReadStatus has no HTTP proxy, redirect or TCP fallback. expectedUID validates
// the server (PID 1 owns the production activation socket), not a payload field.
func ReadStatus(ctx context.Context, socketPath string, expectedUID uint32) (Status, error) {
	var result Status
	if !filepath.IsAbs(socketPath) || filepath.Clean(socketPath) != socketPath {
		return result, errors.New("socket Unix inválido")
	}
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			conn, err := (&net.Dialer{}).DialContext(ctx, "unix", socketPath)
			if err != nil {
				return nil, err
			}
			uid, err := PeerUID(conn)
			if err != nil || uid != expectedUID {
				_ = conn.Close()
				return nil, errors.New("identidade do agente inválida")
			}
			return conn, nil
		},
		DisableKeepAlives: true, MaxResponseHeaderBytes: 4096,
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{
		Transport: transport, Timeout: 3 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	request, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://agent"+StatusPath, nil)
	response, err := client.Do(request)
	if err != nil {
		return result, errors.New("agente indisponível ou identidade inválida")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return result, errors.New("agente recusou a consulta")
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 65537))
	if err != nil || len(data) > 65536 || json.Unmarshal(data, &result) != nil ||
		result.SchemaVersion != 1 || result.Mode != "diagnostic" || result.RuntimeEnabled ||
		len(result.Capabilities) != 1 || result.Capabilities[0] != "status" || result.Report.SchemaVersion != 1 ||
		time.Since(result.Report.ObservedAt) > time.Minute || time.Until(result.Report.ObservedAt) > 5*time.Second {
		return Status{}, errors.New("resposta do agente inválida ou desatualizada")
	}
	return result, nil
}
