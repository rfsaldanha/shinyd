package executor

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDataDirectoriesNeverTraversePublisherLinks(t *testing.T) {
	root := t.TempDir()
	if err := os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	uid := os.Getuid()
	if err := prepareData(root, firstID, uid, uid); err != nil {
		t.Fatal(err)
	}
	files := filepath.Join(root, firstID, "files")
	outside := filepath.Join(t.TempDir(), "protected")
	if err := os.WriteFile(outside, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(files, "escape")); err != nil {
		t.Fatal(err)
	}
	if err := prepareData(root, firstID, uid, uid); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(outside)
	info, _ := os.Stat(outside)
	if string(data) != "keep" || info.Mode().Perm() != 0600 {
		t.Fatal("followed data symlink")
	}
	if prepareData(root, "../../escape", uid, uid) != ErrInvalid {
		t.Fatal("accepted host path")
	}
	if err := os.Symlink(filepath.Dir(outside), filepath.Join(root, secondID)); err != nil {
		t.Fatal(err)
	}
	if prepareData(root, secondID, uid, uid) != ErrDataStorage {
		t.Fatal("accepted app parent symlink")
	}
	if err := os.Remove(filepath.Join(files, "escape")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(files); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Dir(outside), files); err != nil {
		t.Fatal(err)
	}
	if prepareData(root, firstID, uid, uid) != ErrDataStorage {
		t.Fatal("accepted files symlink")
	}
}

func TestDataDirectoriesRefuseExposedRootAndParent(t *testing.T) {
	root := t.TempDir()
	uid := os.Getuid()
	os.Chmod(root, 0755)
	if prepareData(root, firstID, uid, uid) != ErrDataStorage {
		t.Fatal("exposed root")
	}
	os.Chmod(root, 0700)
	if err := prepareData(root, firstID, uid, uid); err != nil {
		t.Fatal(err)
	}
	os.Chmod(filepath.Join(root, firstID), 0777)
	if prepareData(root, firstID, uid, uid) != ErrDataStorage {
		t.Fatal("exposed app parent")
	}
}

func TestOnlyConfirmedRemovalReleasesDataWriterAcrossRecovery(t *testing.T) {
	m, backend, store := fixtureManager(t)
	m.policy.BuilderArtifacts = true
	m.policy.MaxInstances, m.policy.MemoryBudgetMiB = 4, 4096
	m.resolve = func(context.Context, Spec) (string, error) { return testImage, nil }
	spec := artifactSpec()
	spec.DataMode = "persistent"
	ctx := context.Background()
	if v, err := m.Ensure(ctx, firstID, spec); err != nil || v.State != "ready" {
		t.Fatal(v, err)
	}
	if _, err := m.Ensure(ctx, secondID, spec); err != ErrDataInUse {
		t.Fatal("second writer", err)
	}
	m.Stop(ctx, firstID, false)
	if _, err := m.Ensure(ctx, secondID, spec); err != ErrDataInUse {
		t.Fatal("stopped writer released", err)
	}
	backend.foreign = true
	if _, err := m.Retire(ctx, firstID, spec); err != ErrOwnership {
		t.Fatal(err)
	}
	backend.foreign = false
	recovered, err := NewManager(m.policy, store, backend, m.attach, m.admit, m.resolve)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := recovered.Ensure(ctx, secondID, spec); err != ErrDataInUse {
		t.Fatal("tombstone released without removal", err)
	}
	recovered.Recover(ctx)
	if v, err := recovered.Ensure(ctx, secondID, spec); err != nil || v.State != "ready" {
		t.Fatal(v, err)
	}
	if _, err := recovered.Ensure(ctx, firstID, spec); err != ErrConflict {
		t.Fatal("late writer", err)
	}
}

func TestScratchAndOtherAppsDoNotShareWriterReservation(t *testing.T) {
	m, _, _ := fixtureManager(t)
	m.policy.BuilderArtifacts = true
	m.policy.MaxInstances, m.policy.MemoryBudgetMiB = 4, 4096
	m.resolve = func(context.Context, Spec) (string, error) { return testImage, nil }
	spec := artifactSpec()
	spec.DataMode = "persistent"
	ctx := context.Background()
	m.Ensure(ctx, firstID, spec)
	probe := spec
	probe.DataMode = "scratch"
	if v, err := m.Ensure(ctx, secondID, probe); err != nil || v.State != "ready" {
		t.Fatal(v, err)
	}
	other := spec
	other.AppID = secondID
	if v, err := m.Ensure(ctx, "33333333-3333-4333-8333-333333333333", other); err != nil || v.State != "ready" {
		t.Fatal(v, err)
	}
	for _, mode := range []string{"/tmp", "shared", "read-write"} {
		spec.DataMode = mode
		if spec.Validate() != ErrInvalid {
			t.Fatal("accepted arbitrary volume mode")
		}
	}
	legacy := testSpec
	legacy.DataMode = "persistent"
	if legacy.Validate() != ErrInvalid {
		t.Fatal("legacy recipe mounted data")
	}
}

func TestScratchMountNeverReferencesPersistentStorage(t *testing.T) {
	r := Record{ID: firstID, Spec: artifactSpec(), Image: testImage}
	r.Spec.DataMode = "scratch"
	var launch []string
	p := Podman{Run: func(_ context.Context, _ string, args ...string) ([]byte, error) {
		if args[0] == "container" {
			return nil, ErrUnavailable
		}
		if args[0] == "ps" {
			return []byte("[]"), nil
		}
		launch = args
		return nil, nil
	}}
	if err := p.Start(context.Background(), r, nil); err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(launch, " ")
	if strings.Contains(joined, DataRoot) || strings.Contains(joined, "--volume") {
		t.Fatal("scratch accessed real data")
	}
	for _, want := range []string{"--env=SHINYD_DATA_DIR=/data", "--tmpfs=/data:", "--userns=auto:size=65536", "--network=none"} {
		if !strings.Contains(joined, want) {
			t.Fatal("missing isolation", want)
		}
	}
}

func TestPersistentInspectionRejectsWrongVolume(t *testing.T) {
	r := Record{ID: firstID, Spec: artifactSpec(), Image: testImage}
	r.Spec.DataMode = "persistent"
	value := Inspection{Image: testImage}
	value.Config.User = "65534:65534"
	value.Config.Labels = map[string]string{"org.shinyd.managed": "executor-v1", "org.shinyd.instance": firstID, "org.shinyd.spec": signature(r)}
	value.HostConfig.NetworkMode, value.HostConfig.ReadonlyRootfs = "none", true
	value.Mounts = append(value.Mounts, struct {
		Type, Source, Destination string
		RW                        bool
	}{"bind", dataPath(r.Spec.AppID), "/data", true})
	p := Podman{Run: func(context.Context, string, ...string) ([]byte, error) {
		return json.Marshal([]Inspection{value})
	}}
	if _, err := p.Inspect(context.Background(), r); err != nil {
		t.Fatal(err)
	}
	value.Mounts[0].Source = dataPath(secondID)
	if _, err := p.Inspect(context.Background(), r); err != ErrOwnership {
		t.Fatal("foreign volume accepted", err)
	}
	value.Mounts[0].Source = dataPath(r.Spec.AppID)
	value.Mounts[0].RW = false
	if _, err := p.Inspect(context.Background(), r); err != ErrOwnership {
		t.Fatal("read-only mount silently accepted", err)
	}
}
