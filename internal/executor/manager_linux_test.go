package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/rfsaldanha/shinyd/internal/agent"
)

const firstID = "11111111-1111-4111-8111-111111111111"
const secondID = "22222222-2222-4222-8222-222222222222"
const testImage = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

var testSpec = Spec{AppID: firstID, Recipe: "fixture", Priority: "normal"}

type fakeBackend struct {
	mu            sync.Mutex
	values        map[string]*Inspection
	starts, stops int
	fail          bool
	foreign       bool
}

func (f *fakeBackend) Inspect(_ context.Context, r Record) (*Inspection, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.foreign {
		return nil, ErrOwnership
	}
	value := f.values[r.ID]
	if value == nil {
		return nil, nil
	}
	copy := *value
	return &copy, nil
}
func (f *fakeBackend) Start(_ context.Context, r Record) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.starts++
	if f.fail {
		return errors.New("secret from backend")
	}
	value := &Inspection{ID: r.ID}
	value.State.Running = true
	f.values[r.ID] = value
	return nil
}
func (f *fakeBackend) Stop(_ context.Context, r Record) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.foreign {
		return ErrOwnership
	}
	f.stops++
	if value := f.values[r.ID]; value != nil {
		value.State.Running = false
	}
	return nil
}
func (f *fakeBackend) Remove(ctx context.Context, r Record) error {
	if err := f.Stop(ctx, r); err != nil {
		return err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.values, r.ID)
	return nil
}

type fakeEndpoint struct{ closed bool }

func (f *fakeEndpoint) Close() { f.closed = true }
func fixtureManager(t *testing.T) (*Manager, *fakeBackend, *Store) {
	t.Helper()
	store := &Store{dir: t.TempDir(), owner: uint32(os.Getuid())}
	backend := &fakeBackend{values: map[string]*Inspection{}}
	attach := func(_ context.Context, _ Record, i *Inspection) (Endpoint, error) {
		if i == nil || !i.State.Running {
			return nil, ErrUnavailable
		}
		return &fakeEndpoint{}, nil
	}
	manager, err := NewManager(Policy{SchemaVersion: 1, Recipes: map[string]string{"fixture": testImage}, MaxInstances: 2, MemoryBudgetMiB: 1024}, store, backend, attach, func() bool { return true })
	if err != nil {
		t.Fatal(err)
	}
	return manager, backend, store
}
func TestIdempotencyImmutableSpecAndRecovery(t *testing.T) {
	m, backend, store := fixtureManager(t)
	ctx := context.Background()
	for range 2 {
		value, err := m.Ensure(ctx, firstID, testSpec)
		if err != nil || value.State != "ready" {
			t.Fatalf("%+v %v", value, err)
		}
	}
	if backend.starts != 1 {
		t.Fatal("duplicate creation")
	}
	changed := testSpec
	changed.Priority = "high"
	if _, err := m.Ensure(ctx, firstID, changed); err != ErrConflict {
		t.Fatal(err)
	}
	recovered, err := NewManager(m.policy, store, backend, m.attach, m.admit)
	if err != nil {
		t.Fatal(err)
	}
	recovered.Recover(ctx)
	if value, _ := recovered.Get(firstID); value.State != "ready" || backend.starts != 1 {
		t.Fatal("recovery relaunched container")
	}
	if _, err := recovered.Stop(ctx, firstID, false); err != nil {
		t.Fatal(err)
	}
	again, err := NewManager(m.policy, store, backend, m.attach, m.admit)
	if err != nil {
		t.Fatal(err)
	}
	again.Recover(ctx)
	if value, _ := again.Get(firstID); value.State != "stopped" {
		t.Fatal(value)
	}
	if _, err = again.Stop(ctx, firstID, true); err != nil {
		t.Fatal(err)
	}
	if _, err = again.Stop(ctx, firstID, true); err != nil {
		t.Fatal("remove not idempotent")
	}
}
func TestStopPreservesOtherEndpoint(t *testing.T) {
	m, _, _ := fixtureManager(t)
	ctx := context.Background()
	m.Ensure(ctx, firstID, testSpec)
	m.Ensure(ctx, secondID, testSpec)
	other := m.instances[secondID].endpoint.(*fakeEndpoint)
	m.Stop(ctx, firstID, false)
	if other.closed {
		t.Fatal("stopping one instance closed another")
	}
	if value, _ := m.Get(secondID); value.State != "ready" {
		t.Fatal(value)
	}
}
func TestAdmissionAndFailureCircuit(t *testing.T) {
	m, backend, _ := fixtureManager(t)
	ctx := context.Background()
	m.admit = func() bool { return false }
	if _, err := m.Ensure(ctx, firstID, testSpec); err != ErrAdmission || backend.starts != 0 {
		t.Fatal(err)
	}
	m.admit = func() bool { return true }
	backend.fail = true
	for range 3 {
		value, err := m.Ensure(ctx, firstID, testSpec)
		if err != nil || value.State != "failed" || strings.Contains(value.Error, "secret") {
			t.Fatalf("%+v %v", value, err)
		}
	}
	if _, err := m.Ensure(ctx, firstID, testSpec); err != ErrAdmission || backend.starts != 3 {
		t.Fatal("restart loop")
	}
}
func TestForeignResourceNeverStopped(t *testing.T) {
	m, backend, _ := fixtureManager(t)
	ctx := context.Background()
	m.Ensure(ctx, firstID, testSpec)
	backend.foreign = true
	if _, err := m.Stop(ctx, firstID, true); err != ErrOwnership {
		t.Fatal(err)
	}
	if backend.stops != 0 {
		t.Fatal("foreign resource stopped")
	}
}
func TestObserveRevokesDeadInstance(t *testing.T) {
	m, backend, _ := fixtureManager(t)
	ctx := context.Background()
	m.Ensure(ctx, firstID, testSpec)
	endpoint := m.instances[firstID].endpoint.(*fakeEndpoint)
	backend.Stop(ctx, m.instances[firstID].record)
	m.Observe(ctx)
	value, _ := m.Get(firstID)
	if !endpoint.closed || value.State != "failed" || value.Target != "" {
		t.Fatal(value)
	}
	m.Recover(ctx)
	if backend.starts != 1 {
		t.Fatal("crashed container auto-restarted")
	}
}
func TestStrictJSONAndStoreBoundaries(t *testing.T) {
	for _, data := range []string{
		`{"app_id":"x","app_id":"y"}`, `{"recipe":"fixture","command":"id"}`, `{} {}`,
		`{"recipe":"fixture","recipe":"other"}`, `null`,
	} {
		var spec Spec
		if decodeStrict([]byte(data), &spec) == nil && spec.Validate() == nil {
			t.Fatal("accepted", data)
		}
	}
	m, _, store := fixtureManager(t)
	if _, err := m.Ensure(context.Background(), "../../etc/passwd", testSpec); err != ErrInvalid {
		t.Fatal(err)
	}
	path := filepath.Join(store.dir, firstID+".json")
	if err := os.Symlink("/etc/passwd", path); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); err == nil {
		t.Fatal("accepted symlink")
	}
}
func TestConcurrentRequestCreatesOnce(t *testing.T) {
	m, backend, _ := fixtureManager(t)
	var wg sync.WaitGroup
	for range 12 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := m.Ensure(context.Background(), firstID, testSpec)
			if err != nil && err != ErrBusy {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if backend.starts != 1 {
		t.Fatal(backend.starts)
	}
}
func TestAPICredentialsAndArgumentRejection(t *testing.T) {
	m, backend, _ := fixtureManager(t)
	for _, allow := range []bool{true, false} {
		uid := uint32(os.Getuid())
		if !allow {
			uid++
		}
		events := make(chan Event, 16)
		server := NewServer(uid, m, func(event Event) { events <- event })
		path := filepath.Join(t.TempDir(), "socket")
		listener, err := net.Listen("unix", path)
		if err != nil {
			t.Fatal(err)
		}
		go server.Serve(agent.LimitListener(listener, 4))
		client := &http.Client{Timeout: time.Second, Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", path)
		}}}
		for _, body := range []string{
			`{"app_id":"` + firstID + `","recipe":"fixture","priority":"normal","image":"forbidden"}`,
			`{"app_id":"` + firstID + `","recipe":"fixture","priority":"normal","recipe":"evil"}`,
		} {
			request, _ := http.NewRequest("PUT", "http://runtime/v1/instances/"+firstID, strings.NewReader(body))
			request.Header.Set("Content-Type", "application/json")
			request.Header.Set("X-Shinyd-UID", fmt.Sprint(uid))
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			data, _ := io.ReadAll(response.Body)
			response.Body.Close()
			want := 400
			if !allow {
				want = 403
			}
			if response.StatusCode != want {
				t.Fatalf("%d %s", response.StatusCode, data)
			}
			event := <-events
			encoded, _ := json.Marshal(event)
			if strings.Contains(string(encoded), "forbidden") || strings.Contains(string(encoded), "evil") {
				t.Fatal("audit leaked body")
			}
		}
		server.Close()
	}
	if backend.starts != 0 {
		t.Fatal("invalid request reached backend")
	}
}
func TestPodmanArgumentsAreClosedAndImmutable(t *testing.T) {
	var calls [][]string
	p := Podman{Run: func(_ context.Context, binary string, args ...string) ([]byte, error) {
		calls = append(calls, append([]string{binary}, args...))
		if args[0] == "container" {
			return nil, ErrUnavailable
		}
		if args[0] == "ps" {
			return []byte("[]"), nil
		}
		return []byte("created"), nil
	}}
	record := Record{SchemaVersion: 1, ID: firstID, Spec: testSpec, Image: testImage, Desired: "running"}
	if err := p.Start(context.Background(), record); err != nil {
		t.Fatal(err)
	}
	args := strings.Join(calls[len(calls)-1], " ")
	for _, required := range []string{"--pull=never", "--network=none", "--userns=auto:size=65536", "--cap-drop=all", "--read-only", "--image-volume=ignore", "--restart=no", testImage} {
		if !strings.Contains(args, required) {
			t.Fatal("missing", required)
		}
	}
	for _, forbidden := range []string{"--publish", "--volume", "--privileged", "--cpu-quota", "--memory="} {
		if strings.Contains(args, forbidden) {
			t.Fatal("unexpected", forbidden)
		}
	}
}

func TestAppArmorStackRequiresTheExpectedEnforcedProfile(t *testing.T) {
	const profile = "containers-default-0.57.4-apparmor1"
	loaded := profile + " (enforce)\ncrun (unconfined)\n"
	for _, label := range []string{profile + " (enforce)", profile + "//&crun (mixed)\n", "crun//&" + profile + " (mixed)"} {
		if !apparmorEnforced(label, profile, loaded) {
			t.Fatal("rejected enforced container profile:", label)
		}
	}
	for _, trial := range [][3]string{
		{"crun (unconfined)", profile, loaded},
		{profile + " (complain)", profile, loaded},
		{profile + "-other//&crun (mixed)", profile, loaded},
		{profile + "//&crun (mixed)", profile, profile + " (complain)\ncrun (enforce)\n"},
		{profile + "//&crun (mixed)", profile, ""},
		{"unconfined (enforce)", "unconfined", "unconfined (enforce)\n"},
	} {
		if apparmorEnforced(trial[0], trial[1], trial[2]) {
			t.Fatal("accepted missing or unenforced container profile:", trial)
		}
	}
}
