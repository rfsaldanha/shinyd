package executor

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

var ErrBusy = errors.New("runtime_busy")

type Endpoint interface{ Close() }
type Attachment func(context.Context, Record, *Inspection) (Endpoint, error)
type View struct {
	ID         string `json:"id"`
	AppID      string `json:"app_id"`
	Recipe     string `json:"recipe"`
	Priority   string `json:"priority"`
	Desired    string `json:"desired"`
	State      string `json:"state"`
	Target     string `json:"target,omitempty"`
	Error      string `json:"error,omitempty"`
	Artifact   string `json:"artifact,omitempty"`
	SourceHash string `json:"source_hash,omitempty"`
	Image      string `json:"image,omitempty"`
}
type managed struct {
	record        Record
	endpoint      Endpoint
	state, reason string
}
type Manager struct {
	policy    Policy
	store     *Store
	backend   Backend
	attach    Attachment
	admit     func() bool
	mu        sync.Mutex
	operation sync.Mutex
	instances map[string]*managed
	resolve   ArtifactResolver
}

func NewManager(policy Policy, store *Store, backend Backend, attach Attachment, admit func() bool, resolvers ...ArtifactResolver) (*Manager, error) {
	records, err := store.Load()
	if err != nil {
		return nil, err
	}
	m := &Manager{policy: policy, store: store, backend: backend, attach: attach, admit: admit, instances: map[string]*managed{}}
	if len(resolvers) == 1 {
		m.resolve = resolvers[0]
	}
	for id, record := range records {
		m.instances[id] = &managed{record: record, state: "unavailable"}
	}
	return m, nil
}
func (m *Manager) view(item *managed) View {
	r := item.record
	v := View{ID: r.ID, AppID: r.Spec.AppID, Recipe: r.Spec.Recipe, Priority: r.Spec.Priority, Desired: r.Desired, State: item.state, Error: item.reason}
	if r.Spec.Artifact != "" {
		v.Artifact, v.SourceHash, v.Image = r.Spec.Artifact, r.Spec.SourceHash, r.Image
	}
	if item.state == "ready" {
		v.Target = Target(r.ID)
	}
	return v
}
func (m *Manager) Get(id string) (View, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	item, ok := m.instances[id]
	if !ok {
		return View{}, ErrNotFound
	}
	return m.view(item), nil
}
func (m *Manager) set(item *managed, state, reason string) {
	m.mu.Lock()
	item.state = state
	item.reason = reason
	m.mu.Unlock()
}
func (m *Manager) save(item *managed, record Record) error {
	if err := m.store.Save(record); err != nil {
		return ErrUnavailable
	}
	m.mu.Lock()
	item.record = record
	m.mu.Unlock()
	return nil
}
func (m *Manager) Ensure(ctx context.Context, id string, spec Spec) (View, error) {
	if !IDPattern.MatchString(id) || spec.Validate() != nil {
		return View{}, ErrInvalid
	}
	if !m.operation.TryLock() {
		return View{}, ErrBusy
	}
	defer m.operation.Unlock()
	m.mu.Lock()
	item, exists := m.instances[id]
	if exists && (item.record.Spec != spec || item.record.Desired == "removed") {
		m.mu.Unlock()
		return View{}, ErrConflict
	}
	if !exists && len(m.instances) >= 256 {
		m.mu.Unlock()
		return View{}, ErrAdmission
	}
	others := 0
	for otherID, other := range m.instances {
		if otherID != id && (other.record.Desired == "running" || other.state == "failed" || other.state == "unavailable" || other.state == "stopping") {
			others++
		}
	}
	m.mu.Unlock()
	// Artifact launches are once-only. Retries observe the original outcome;
	// a failed/stopped candidate needs a new publication and UUID.
	if exists && spec.Artifact != "" {
		return m.Get(id)
	}
	image, approved := m.policy.Recipes[spec.Recipe]
	if spec.Artifact != "" {
		if !m.policy.BuilderArtifacts || m.resolve == nil {
			return View{}, ErrArtifact
		}
		var err error
		image, err = m.resolve(ctx, spec)
		if err != nil {
			return View{}, ErrArtifact
		}
		approved = imagePattern.MatchString(image)
	}
	if !approved {
		return View{}, ErrInvalid
	}
	if exists && item.record.Image != image {
		return View{}, ErrConflict
	}
	if exists {
		value, err := m.backend.Inspect(ctx, item.record)
		if err != nil {
			return View{}, err
		}
		if value != nil && value.State.Running && item.endpoint != nil && item.record.Desired == "running" {
			return m.Get(id)
		}
	}
	if others >= m.policy.MaxInstances || (others+1)*512 > m.policy.MemoryBudgetMiB || !m.admit() {
		return View{}, ErrAdmission
	}
	if !exists {
		item = &managed{record: Record{SchemaVersion: 1, ID: id, Spec: spec, Image: image, Desired: "stopped"}, state: "stopped"}
	}
	record := item.record
	recent := []time.Time{}
	for _, attempt := range record.Attempts {
		if time.Since(attempt) < 10*time.Minute {
			recent = append(recent, attempt)
		}
	}
	if len(recent) >= 3 {
		return View{}, ErrAdmission
	}
	record.Attempts = append(recent, time.Now().UTC())
	record.Desired = "running"
	if err := m.save(item, record); err != nil {
		return View{}, err
	}
	if !exists {
		m.mu.Lock()
		m.instances[id] = item
		m.mu.Unlock()
	}
	m.set(item, "starting", "")
	if item.endpoint != nil {
		item.endpoint.Close()
		item.endpoint = nil
	}
	err := m.backend.Start(ctx, record)
	if err == nil {
		var value *Inspection
		value, err = m.backend.Inspect(ctx, record)
		if err == nil {
			item.endpoint, err = m.attach(ctx, record, value)
		}
	}
	if err != nil {
		// Do not stop a foreign container on an ownership mismatch. Backend.Stop
		// verifies ownership again, including when creation had an ambiguous result.
		_ = m.backend.Stop(ctx, record)
		m.set(item, "failed", safeError(err))
		return m.Get(id) // A failed attempt is persisted and visible, not retried in a loop.
	}
	m.set(item, "ready", "")
	return m.Get(id)
}
func (m *Manager) Stop(ctx context.Context, id string, remove bool) (View, error) {
	if !IDPattern.MatchString(id) {
		return View{}, ErrInvalid
	}
	if !m.operation.TryLock() {
		return View{}, ErrBusy
	}
	defer m.operation.Unlock()
	m.mu.Lock()
	item, ok := m.instances[id]
	m.mu.Unlock()
	if !ok {
		if remove {
			return View{ID: id, State: "removed"}, nil
		}
		return View{}, ErrNotFound
	}
	if item.record.Spec.Artifact != "" && (remove || item.record.Desired == "removed") {
		return m.retire(ctx, item)
	}
	record := item.record
	record.Desired = "stopped"
	if err := m.save(item, record); err != nil {
		return View{}, err
	}
	m.set(item, "stopping", "")
	if item.endpoint != nil {
		item.endpoint.Close()
		item.endpoint = nil
	}
	var err error
	if remove {
		err = m.backend.Remove(ctx, record)
	} else {
		err = m.backend.Stop(ctx, record)
	}
	if err != nil {
		m.set(item, "failed", safeError(err))
		return View{}, err
	}
	if remove {
		if err = m.store.Remove(id); err != nil {
			return View{}, ErrUnavailable
		}
		m.mu.Lock()
		delete(m.instances, id)
		m.mu.Unlock()
		return View{ID: id, State: "removed"}, nil
	}
	m.set(item, "stopped", "")
	return m.Get(id)
}
func safeError(err error) string {
	for _, allowed := range []error{ErrInvalid, ErrConflict, ErrAdmission, ErrOwnership, ErrNotFound, ErrBusy,
		ErrProcess, ErrProfile, ErrMapping, ErrCgroup, ErrNamespace, ErrReadiness, ErrArtifact} {
		if errors.Is(err, allowed) {
			return allowed.Error()
		}
	}
	return "runtime_unavailable"
}

// Recover restores only relays for already running, owned containers. It never
// launches stopped/crashed containers on daemon startup or invents restart loops.
func (m *Manager) Recover(ctx context.Context) {
	m.operation.Lock()
	defer m.operation.Unlock()
	for _, item := range m.instances {
		r := item.record
		if r.Desired == "removed" {
			_, _ = m.retire(ctx, item)
			continue
		}
		if r.Desired == "stopped" {
			err := m.backend.Stop(ctx, r)
			if err != nil {
				m.set(item, "failed", safeError(err))
			} else {
				m.set(item, "stopped", "")
			}
			continue
		}
		value, err := m.backend.Inspect(ctx, r)
		if err == nil {
			item.endpoint, err = m.attach(ctx, r, value)
		}
		if err != nil {
			m.set(item, "failed", safeError(err))
			continue
		}
		m.set(item, "ready", "")
	}
}

// A durable tombstone fences delayed PUTs, even if cancellation arrived before
// the first request. Do not delete it while workers could still hold this ID.
func (m *Manager) Retire(ctx context.Context, id string, spec Spec) (View, error) {
	if !IDPattern.MatchString(id) || spec.Validate() != nil || spec.Artifact == "" {
		return View{}, ErrInvalid
	}
	if !m.operation.TryLock() {
		return View{}, ErrBusy
	}
	defer m.operation.Unlock()
	m.mu.Lock()
	item, exists := m.instances[id]
	if exists && item.record.Spec != spec {
		m.mu.Unlock()
		return View{}, ErrConflict
	}
	if !exists && len(m.instances) >= 256 {
		m.mu.Unlock()
		return View{}, ErrAdmission
	}
	m.mu.Unlock()
	if !exists {
		item = &managed{record: Record{SchemaVersion: 1, ID: id, Spec: spec, Desired: "removed"}, state: "removing"}
		if err := m.store.Save(item.record); err != nil {
			return View{}, ErrUnavailable
		}
		m.mu.Lock()
		m.instances[id] = item
		m.mu.Unlock()
	}
	return m.retire(ctx, item)
}

func (m *Manager) retire(ctx context.Context, item *managed) (View, error) {
	record := item.record
	record.Desired = "removed"
	if err := m.save(item, record); err != nil {
		return View{}, err
	}
	if item.endpoint != nil {
		item.endpoint.Close()
		item.endpoint = nil
	}
	m.set(item, "removing", "")
	// An image-less tombstone was persisted before any launch was accepted.
	if record.Image != "" {
		if err := m.backend.Remove(ctx, record); err != nil {
			m.set(item, "failed", safeError(err))
			return View{}, err
		}
	}
	m.set(item, "removed", "")
	return m.Get(record.ID)
}

// Observe revokes the relay if the container exits. Its pinned network
// namespace is never reused for a replacement process.
func (m *Manager) Observe(ctx context.Context) {
	if !m.operation.TryLock() {
		return
	}
	defer m.operation.Unlock()
	for _, item := range m.instances {
		if item.endpoint == nil {
			continue
		}
		value, err := m.backend.Inspect(ctx, item.record)
		if err != nil || value == nil || !value.State.Running {
			item.endpoint.Close()
			item.endpoint = nil
			m.set(item, "failed", safeError(ErrUnavailable))
		}
	}
}
func (m *Manager) Close() {
	m.operation.Lock()
	defer m.operation.Unlock()
	for _, item := range m.instances {
		if item.endpoint != nil {
			item.endpoint.Close()
			item.endpoint = nil
		}
	}
	m.store.Close()
}
func RealAttachment(appsDir string, gid int) Attachment {
	slots := make(chan struct{}, 128)
	return func(ctx context.Context, r Record, value *Inspection) (Endpoint, error) {
		deadline := time.Now().Add(20 * time.Second)
		var namespace *os.File
		var err error
		for {
			// OCI "running" can precede exec and its AppArmor transition.
			// No traffic is admitted until every kernel check succeeds.
			namespace, err = PinNamespace(value, r)
			if err == nil {
				break
			}
			if time.Now().After(deadline) || ctx.Err() != nil {
				return nil, err
			}
			select {
			case <-ctx.Done():
				return nil, err
			case <-time.After(100 * time.Millisecond):
			}
		}
		transport := &http.Transport{
			DialContext:       func(ctx context.Context, _, _ string) (net.Conn, error) { return DialNamespace(ctx, namespace) },
			DisableKeepAlives: true, MaxResponseHeaderBytes: 16384,
		}
		defer transport.CloseIdleConnections()
		client := &http.Client{Transport: transport, Timeout: time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		for {
			req, _ := http.NewRequestWithContext(ctx, "GET", "http://instance/", nil)
			response, err := client.Do(req)
			if err == nil {
				response.Body.Close()
				if response.StatusCode >= 200 && response.StatusCode < 300 {
					break
				}
			}
			if time.Now().After(deadline) || ctx.Err() != nil {
				namespace.Close()
				return nil, ErrReadiness
			}
			select {
			case <-ctx.Done():
				namespace.Close()
				return nil, ErrReadiness
			case <-time.After(100 * time.Millisecond):
			}
		}
		relay, err := NewRelay(filepath.Join(appsDir, r.ID+".sock"), namespace, gid, slots)
		if err != nil {
			namespace.Close()
			return nil, err
		}
		return relay, nil
	}
}
func AdmissionAvailable() bool {
	total, available, err := Memory()
	if err != nil || available < max(uint64(1<<30), total*15/100)+(512<<20) {
		return false
	}
	pressure, err := os.ReadFile("/proc/pressure/memory")
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(pressure), "\n") {
		if strings.HasPrefix(line, "full ") {
			var avg float64
			if _, err = fmt.Sscanf(line, "full avg10=%f", &avg); err != nil {
				return false
			}
			return avg <= 1
		}
	}
	return false
}
