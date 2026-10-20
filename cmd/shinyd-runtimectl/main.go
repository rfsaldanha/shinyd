package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/rfsaldanha/shinyd/internal/agent"
	"github.com/rfsaldanha/shinyd/internal/executor"
)

func run() error {
	flag.Parse()
	if flag.NArg() != 2 || !executor.IDPattern.MatchString(flag.Arg(1)) {
		return fmt.Errorf("uso: shinyd-runtimectl {ensure|inspect|stop|remove} UUID; ensure lê JSON da entrada padrão")
	}
	method := "GET"
	path := "/v1/instances/" + flag.Arg(1)
	var body []byte
	var err error
	switch flag.Arg(0) {
	case "inspect":
	case "ensure":
		method = "PUT"
		body, err = io.ReadAll(io.LimitReader(os.Stdin, 1025))
		if err != nil || len(body) > 1024 {
			return executor.ErrInvalid
		}
	case "stop":
		method = "POST"
		path += "/stop"
	case "remove":
		method = "DELETE"
	default:
		return executor.ErrInvalid
	}
	transport := &http.Transport{DisableKeepAlives: true, MaxResponseHeaderBytes: 4096,
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			c, err := (&net.Dialer{}).DialContext(ctx, "unix", "/run/shinyd-runtime/control.sock")
			if err != nil {
				return nil, err
			}
			uid, err := agent.PeerUID(c)
			if err != nil || uid != 0 {
				c.Close()
				return nil, executor.ErrUnavailable
			}
			return c, nil
		}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 65 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	request, _ := http.NewRequest(method, "http://runtime"+path, bytes.NewReader(body))
	if method == "PUT" {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.Do(request)
	if err != nil {
		return executor.ErrUnavailable
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 8193))
	if err != nil || len(data) > 8192 {
		return executor.ErrUnavailable
	}
	if response.StatusCode != 200 {
		return fmt.Errorf("executor recusou a operação (HTTP %d)", response.StatusCode)
	}
	_, err = os.Stdout.Write(data)
	return err
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
