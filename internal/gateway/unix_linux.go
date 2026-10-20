package gateway

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/url"

	"github.com/rfsaldanha/shinyd/internal/agent"
)

func validUnixTarget(target *url.URL, id string) bool {
	return instanceID.MatchString(id) && target.Scheme == "unix" && target.Host == "" &&
		target.Opaque == "" && target.RawPath == "" && target.User == nil && target.RawQuery == "" &&
		!target.ForceQuery && target.Fragment == "" && target.Path == "/run/shinyd-runtime/apps/"+id+".sock"
}
func unixTransport(path string, uid uint32) *http.Transport {
	return &http.Transport{
		DisableKeepAlives: true,
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			conn, err := (&net.Dialer{}).DialContext(ctx, "unix", path)
			if err != nil {
				return nil, err
			}
			peer, err := agent.PeerUID(conn)
			if err != nil || peer != uid {
				conn.Close()
				return nil, errors.New("runtime identity mismatch")
			}
			return conn, nil
		},
	}
}
