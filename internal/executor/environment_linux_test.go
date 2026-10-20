package executor

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

func TestEnvironmentValidation(t *testing.T) {
	spec := artifactSpec()
	spec.ConfigRevision = 1
	valid := map[string]string{"TOKEN": "quotes\"'\nç\\=value", "EMPTY": ""}
	if validateEnvironment(spec, valid) != nil {
		t.Fatal("valid environment rejected")
	}
	for _, invalid := range []map[string]string{
		nil, {"PORT": "9"}, {"RHOME": "/tmp"}, {"LD_PRELOAD": "x"}, {"SHINYD_INSTANCE_ID": "x"},
		{"RENV_PATHS_LIBRARY": "x"}, {"KEY": "nul\x00"}, {"KEY": "\xff"},
		{"KEY": strings.Repeat("ç", 8193)}, {"BAD-NAME": "x"},
	} {
		if validateEnvironment(spec, invalid) != ErrInvalid {
			t.Fatal("unsafe environment accepted")
		}
	}
	large := map[string]string{"A": strings.Repeat("x", 16384), "B": strings.Repeat("x", 16384), "C": strings.Repeat("x", 16384), "D": strings.Repeat("x", 16384)}
	if validateEnvironment(spec, large) != ErrInvalid {
		t.Fatal("oversized combined environment accepted")
	}
	if validateEnvironment(testSpec, valid) != ErrInvalid {
		t.Fatal("legacy recipe accepted environment")
	}
	if validateEnvironment(spec, map[string]string{}) != nil {
		t.Fatal("empty revision rejected")
	}
}

func TestEnvironmentFileIsSealedAndInheritedWithoutArguments(t *testing.T) {
	values := map[string]string{"TOKEN": "private-token-ç\nquoted\"value"}
	file, err := environmentFile(values)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err = file.WriteAt([]byte("changed"), 0); err == nil {
		t.Fatal("unsealed environment")
	}
	if err = unix.Ftruncate(int(file.Fd()), 0); err == nil {
		t.Fatal("could truncate environment")
	}
	data, err := runCommand(context.Background(), "/bin/cat", []*os.File{file}, "/proc/self/fd/3")
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]string
	if json.Unmarshal(data, &got) != nil || !reflect.DeepEqual(got, values) {
		t.Fatal("inherited payload differs")
	}
}

func TestSecretPodmanLaunchNeverInterpolatesValues(t *testing.T) {
	spec := artifactSpec()
	spec.ConfigRevision = 2
	r := Record{ID: firstID, Spec: spec, Image: testImage}
	values := map[string]string{"TOKEN": "private-shell-'\";$(exit 1)\nç"}
	called := false
	p := Podman{Run: func(_ context.Context, _ string, args ...string) ([]byte, error) {
		if args[0] == "container" {
			return nil, ErrUnavailable
		}
		if args[0] == "ps" {
			return []byte("[]"), nil
		}
		t.Fatal("secret launch used ordinary runner")
		return nil, ErrUnavailable
	}, RunEnvironment: func(_ context.Context, env map[string]string, args ...string) ([]byte, error) {
		called = true
		if !reflect.DeepEqual(env, values) {
			t.Fatal("payload differs")
		}
		joined := strings.Join(args, " ")
		if strings.Contains(joined, values["TOKEN"]) || strings.Contains(joined, "--env=TOKEN") {
			t.Fatal("plaintext arguments")
		}
		for _, required := range []string{"--runtime=/usr/bin/crun", "--preserve-fds=1", "--entrypoint=Rscript", "--userns=auto:size=65536", "--network=none", "--cap-drop=all", "--read-only"} {
			if !strings.Contains(joined, required) {
				t.Fatal("missing protection", required)
			}
		}
		return nil, nil
	}}
	if p.Start(context.Background(), r, values) != nil || !called {
		t.Fatal("launch not delivered")
	}
}

func TestEnvironmentAPIPersistsOnlyRevisionAndNeverReturnsValues(t *testing.T) {
	m, backend, store := fixtureManager(t)
	m.policy.BuilderArtifacts = true
	m.resolve = func(context.Context, Spec) (string, error) { return testImage, nil }
	spec := artifactSpec()
	spec.ConfigRevision = 1
	var audit []Event
	server := NewServer(123, m, func(e Event) { audit = append(audit, e) })
	call := func(method, suffix string, input any) *httptest.ResponseRecorder {
		body, _ := json.Marshal(input)
		r := httptest.NewRequest(method, "/v1/instances/"+firstID+suffix, strings.NewReader(string(body)))
		r.Host = "runtime"
		r.Header.Set("Content-Type", "application/json")
		r = r.WithContext(context.WithValue(r.Context(), callerKey{}, caller{123, true}))
		w := httptest.NewRecorder()
		server.Handler.ServeHTTP(w, r)
		return w
	}
	input := struct {
		Spec
		Environment map[string]string `json:"environment"`
	}{spec, map[string]string{"TOKEN": "private-sentinel"}}
	for range 2 {
		w := call(http.MethodPut, "", input)
		if w.Code != 200 || strings.Contains(w.Body.String(), "private-sentinel") || strings.Contains(w.Body.String(), "environment") {
			t.Fatal("unsafe response", w.Code)
		}
	}
	if backend.starts != 1 {
		t.Fatal("duplicate launch")
	}
	data, _ := json.Marshal(audit)
	records, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	state, _ := json.Marshal(records)
	if strings.Contains(string(data)+string(state), "private-sentinel") {
		t.Fatal("persisted secret")
	}
	if records[firstID].Spec.ConfigRevision != 1 {
		t.Fatal("revision lost")
	}
	changed := spec
	changed.ConfigRevision = 2
	input.Spec = changed
	if w := call(http.MethodPut, "", input); w.Code != 409 {
		t.Fatal("configuration changed existing instance")
	}
	// Cleanup requires only the non-sensitive spec, including after losing the key.
	if w := call(http.MethodPost, "/retire", spec); w.Code != 200 {
		t.Fatal("retirement requires secrets")
	}
	input.Spec = spec
	if w := call(http.MethodPut, "", input); w.Code != 409 {
		t.Fatal("late start crossed retirement fence")
	}
}
