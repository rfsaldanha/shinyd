package executor

import (
	"context"
	"encoding/json"
	"path/filepath"
	"regexp"
	"strings"
)

var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

// The panel supplies a key, never a filesystem path or an image reference.
// Manifests and every ancestor belong to root; the builder writes atomically.
type ArtifactResolver func(context.Context, Spec) (string, error)

type artifactManifest struct {
	SchemaVersion int               `json:"schema_version"`
	RecipeVersion int               `json:"recipe_version"`
	AppID         string            `json:"app_id"`
	CacheKey      string            `json:"cache_key"`
	SourceHash    string            `json:"source_hash"`
	Image         string            `json:"image"`
	BaseImage     string            `json:"base_image"`
	Entrypoint    string            `json:"entrypoint"`
	RVersion      string            `json:"r_version"`
	LockHash      string            `json:"lock_hash"`
	CreatedAt     string            `json:"created_at"`
	Packages      map[string]string `json:"packages"`
	Protection    struct {
		AppArmor    string `json:"apparmor"`
		MappedUID   bool   `json:"mapped_uid"`
		Network     string `json:"network"`
		Maintenance bool   `json:"maintenance"`
	} `json:"protection"`
	CacheHit bool `json:"cache_hit"`
}

func resolveArtifact(ctx context.Context, dir string, owner uint32, spec Spec, run Runner) (string, error) {
	if spec.Validate() != nil || spec.Artifact == "" || SecureDir(dir, owner) != nil {
		return "", ErrArtifact
	}
	data, err := privateRead(filepath.Join(dir, spec.Artifact+".json"), owner, 262144)
	var manifest artifactManifest
	if err != nil || decodeStrict(data, &manifest) != nil ||
		manifest.SchemaVersion != 1 || manifest.RecipeVersion != 1 ||
		manifest.AppID != spec.AppID || manifest.CacheKey != spec.Artifact ||
		manifest.SourceHash != spec.SourceHash || !imagePattern.MatchString(manifest.Image) ||
		!imagePattern.MatchString(manifest.BaseImage) || !digestPattern.MatchString(manifest.LockHash) ||
		len(manifest.Packages) == 0 || len(manifest.Packages) > 2000 ||
		!manifest.Protection.MappedUID || !manifest.Protection.Maintenance ||
		manifest.Protection.Network != "none" || manifest.Protection.AppArmor == "" {
		return "", ErrArtifact
	}
	raw, err := run(ctx, "/usr/bin/podman", "image", "inspect", manifest.Image)
	var images []struct {
		ID     string `json:"Id"`
		Config struct {
			Labels map[string]string `json:"Labels"`
		} `json:"Config"`
	}
	if err != nil || json.Unmarshal(raw, &images) != nil || len(images) != 1 ||
		"sha256:"+strings.TrimPrefix(images[0].ID, "sha256:") != manifest.Image ||
		images[0].Config.Labels["org.shinyd.key"] != spec.Artifact ||
		images[0].Config.Labels["org.shinyd.artifact"] != "v1" {
		return "", ErrArtifact
	}
	return manifest.Image, nil
}

func BuilderArtifactResolver(run Runner) ArtifactResolver {
	return func(ctx context.Context, spec Spec) (string, error) {
		return resolveArtifact(ctx, "/var/lib/shinyd-builder/artifacts", 0, spec, run)
	}
}
