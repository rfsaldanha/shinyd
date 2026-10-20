package executor

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func artifactSpec() Spec {
	return Spec{AppID: firstID, Priority: "normal", Artifact: strings.Repeat("b", 64), SourceHash: strings.Repeat("c", 64)}
}

func TestArtifactRequiresTrustedManifestAndMatchingImage(t *testing.T) {
	// /tmp and shared workspace ancestors intentionally fail SecureDir.
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	base, err := os.MkdirTemp(home, ".shinyd-artifact-test-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(base)
	dir, _ := filepath.Abs(base)
	s := artifactSpec()
	manifest := artifactManifest{SchemaVersion: 1, RecipeVersion: 1, AppID: s.AppID, CacheKey: s.Artifact,
		SourceHash: s.SourceHash, Image: testImage, BaseImage: testImage, LockHash: s.SourceHash,
		Packages: map[string]string{"shiny": "1.10.0"}}
	manifest.Protection.MappedUID = true
	manifest.Protection.Maintenance = true
	manifest.Protection.Network = "none"
	manifest.Protection.AppArmor = "test (enforce)"
	data, _ := json.Marshal(manifest)
	path := filepath.Join(dir, s.Artifact+".json")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	run := func(_ context.Context, binary string, args ...string) ([]byte, error) {
		if binary != "/usr/bin/podman" || strings.Join(args, " ") != "image inspect "+testImage {
			t.Fatal(binary, args)
		}
		return []byte(`[{"Id":"` + testImage + `","Config":{"Labels":{"org.shinyd.key":"` + s.Artifact + `","org.shinyd.artifact":"v1"}}}]`), nil
	}
	resolve := func(spec Spec) (string, error) {
		return resolveArtifact(context.Background(), dir, uint32(os.Getuid()), spec, run)
	}
	if image, err := resolve(s); err != nil || image != testImage {
		t.Fatal(image, err)
	}
	other := s
	other.AppID = secondID
	if _, err := resolve(other); err != ErrArtifact {
		t.Fatal("cross-app artifact", err)
	}
	other = s
	other.SourceHash = strings.Repeat("d", 64)
	if _, err := resolve(other); err != ErrArtifact {
		t.Fatal("wrong source", err)
	}
	os.Chmod(path, 0666)
	if _, err := resolve(s); err != ErrArtifact {
		t.Fatal("writable manifest", err)
	}
	os.Chmod(path, 0600)
	os.Link(path, path+".hardlink")
	if _, err := resolve(s); err != ErrArtifact {
		t.Fatal("hardlink", err)
	}
	os.Remove(path + ".hardlink")
	os.Rename(path, path+".real")
	os.Symlink(path+".real", path)
	if _, err := resolve(s); err != ErrArtifact {
		t.Fatal("symlink", err)
	}
	os.Remove(path)
	os.Rename(path+".real", path)
	os.WriteFile(path, []byte(strings.Replace(string(data), `"schema_version":1`, `"schema_version":1,"schema_version":1`, 1)), 0600)
	if _, err := resolve(s); err != ErrArtifact {
		t.Fatal("duplicate keys", err)
	}
	os.WriteFile(path, data, 0600)
	bad := func(context.Context, string, ...string) ([]byte, error) {
		return []byte(`[{"Id":"` + testImage + `","Config":{"Labels":{}}}]`), nil
	}
	if _, err := resolveArtifact(context.Background(), dir, uint32(os.Getuid()), s, bad); err != ErrArtifact {
		t.Fatal("unsealed image", err)
	}
}

func TestArtifactLaunchOnceAndRetirementFenceSurvivesRecovery(t *testing.T) {
	m, b, store := fixtureManager(t)
	m.policy.BuilderArtifacts = true
	m.resolve = func(context.Context, Spec) (string, error) { return testImage, nil }
	s, ctx := artifactSpec(), context.Background()
	for range 2 {
		if value, err := m.Ensure(ctx, firstID, s); err != nil || value.State != "ready" {
			t.Fatal(value, err)
		}
	}
	if b.starts != 1 {
		t.Fatal("duplicate launch")
	}
	b.Stop(ctx, m.instances[firstID].record)
	m.Observe(ctx)
	if value, err := m.Ensure(ctx, firstID, s); err != nil || value.State != "failed" || b.starts != 1 {
		t.Fatal("restarted failed candidate", value, err)
	}
	if _, err := m.Retire(ctx, firstID, s); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Retire(ctx, secondID, s); err != nil {
		t.Fatal(err)
	}
	again, err := NewManager(m.policy, store, b, m.attach, m.admit, m.resolve)
	if err != nil {
		t.Fatal(err)
	}
	again.Recover(ctx)
	for _, id := range []string{firstID, secondID} {
		if _, err := again.Ensure(ctx, id, s); err != ErrConflict {
			t.Fatal("late launch resurrected", id, err)
		}
		if value, err := again.Stop(ctx, id, true); err != nil || value.State != "removed" {
			t.Fatal(value, err)
		}
	}
	if b.starts != 1 {
		t.Fatal("launch on recovery")
	}
	if _, err := store.Load(); err != nil {
		t.Fatal("invalid tombstone", err)
	}
}

func TestArtifactOptInAndCandidateFailurePreserveExistingProcess(t *testing.T) {
	m, b, _ := fixtureManager(t)
	ctx := context.Background()
	if _, err := m.Ensure(ctx, firstID, testSpec); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Ensure(ctx, secondID, artifactSpec()); err != ErrArtifact {
		t.Fatal(err)
	}
	m.policy.BuilderArtifacts = true
	m.resolve = func(context.Context, Spec) (string, error) { return testImage, nil }
	b.fail = true
	value, err := m.Ensure(ctx, secondID, artifactSpec())
	if err != nil || value.State != "failed" {
		t.Fatal(value, err)
	}
	if value, _ := m.Get(firstID); value.State != "ready" || !b.values[firstID].State.Running {
		t.Fatal("old publication affected")
	}
	if _, err := m.Ensure(ctx, secondID, artifactSpec()); err != nil || b.starts != 2 {
		t.Fatal("candidate automatically relaunched", err)
	}
}
