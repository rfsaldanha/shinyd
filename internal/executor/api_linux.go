package executor

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/rfsaldanha/shinyd/internal/agent"
)

type callerKey struct{}
type caller struct {
	uid   uint32
	valid bool
}
type Event struct {
	Event      string `json:"event"`
	Operation  string `json:"operation"`
	InstanceID string `json:"instance_id,omitempty"`
	CallerUID  uint32 `json:"caller_uid"`
	Result     int    `json:"result"`
}

func NewServer(uid uint32, manager *Manager, audit func(Event)) *http.Server {
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Connection", "close")
		peer, ok := r.Context().Value(callerKey{}).(caller)
		event := Event{Event: "runtime.request", Operation: "rejected", CallerUID: peer.uid, Result: 403}
		defer func() {
			if audit != nil {
				audit(event)
			}
		}()
		fail := func(status int, err error) {
			event.Result = status
			w.WriteHeader(status)
			json.NewEncoder(w).Encode(map[string]string{"error": safeError(err)})
		}
		if !ok || !peer.valid || peer.uid != uid {
			fail(403, ErrInvalid)
			return
		}
		if r.Host != "runtime" || r.URL.IsAbs() || r.URL.RawPath != "" || r.URL.ForceQuery || r.URL.RawQuery != "" ||
			r.Header.Get("Upgrade") != "" || len(r.TransferEncoding) != 0 {
			fail(400, ErrInvalid)
			return
		}
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/v1/instances/"), "/")
		if !strings.HasPrefix(r.URL.Path, "/v1/instances/") || len(parts) < 1 || !IDPattern.MatchString(parts[0]) {
			fail(404, ErrNotFound)
			return
		}
		id := parts[0]
		event.InstanceID = id
		var value View
		var err error
		// Mutations have their own deadline. A disconnected client can retry the
		// same ID; losing the response cannot create a duplicate container.
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		switch {
		case len(parts) == 1 && r.Method == "GET" && r.ContentLength == 0:
			event.Operation = "inspect"
			value, err = manager.Get(id)
		case (len(parts) == 1 && r.Method == "PUT") || (len(parts) == 2 && parts[1] == "retire" && r.Method == "POST"):
			event.Operation = "ensure"
			if r.Header.Get("Content-Type") != "application/json" || r.ContentLength < 1 || r.ContentLength > maxEnvironmentRequest {
				fail(400, ErrInvalid)
				return
			}
			body, readErr := io.ReadAll(http.MaxBytesReader(w, r.Body, maxEnvironmentRequest))
			defer clear(body)
			var input struct {
				Spec
				Environment map[string]string `json:"environment,omitempty"`
			}
			if readErr != nil || !utf8.Valid(body) || decodeStrict(body, &input) != nil || input.Spec.Validate() != nil {
				fail(400, ErrInvalid)
				return
			}
			if len(parts) == 2 {
				if input.Environment != nil {
					fail(400, ErrInvalid)
					return
				}
				event.Operation = "retire"
				value, err = manager.Retire(ctx, id, input.Spec)
			} else {
				value, err = manager.Ensure(ctx, id, input.Spec, input.Environment)
			}
		case len(parts) == 2 && parts[1] == "stop" && r.Method == "POST" && r.ContentLength == 0:
			event.Operation = "stop"
			value, err = manager.Stop(ctx, id, false)
		case len(parts) == 1 && r.Method == "DELETE" && r.ContentLength == 0:
			event.Operation = "remove"
			value, err = manager.Stop(ctx, id, true)
		default:
			fail(400, ErrInvalid)
			return
		}
		if err != nil {
			status := 503
			switch err {
			case ErrInvalid, ErrArtifact:
				status = 400
			case ErrConflict, ErrOwnership:
				status = 409
			case ErrNotFound:
				status = 404
			case ErrAdmission, ErrBusy:
				status = 429
				w.Header().Set("Retry-After", "5")
			}
			fail(status, err)
			return
		}
		event.Result = 200
		json.NewEncoder(w).Encode(value)
	})
	s := &http.Server{Handler: handler, ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 3 * time.Second, WriteTimeout: 65 * time.Second,
		MaxHeaderBytes: 4096, ErrorLog: log.New(io.Discard, "", 0),
		ConnContext: func(ctx context.Context, c net.Conn) context.Context {
			uid, err := agent.PeerUID(c)
			return context.WithValue(ctx, callerKey{}, caller{uid, err == nil})
		}}
	s.SetKeepAlivesEnabled(false)
	return s
}
