// Package executor owns a narrow, local container execution contract.
package executor

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"syscall"
)

var (
	IDPattern      = regexp.MustCompile(`^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$`)
	recipePattern  = regexp.MustCompile(`^[a-z][a-z0-9-]{0,39}$`)
	imagePattern   = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
	ErrInvalid     = errors.New("invalid_request")
	ErrConflict    = errors.New("instance_conflict")
	ErrUnavailable = errors.New("runtime_unavailable")
	ErrAdmission   = errors.New("admission_denied")
	ErrOwnership   = errors.New("ownership_mismatch")
	ErrNotFound    = errors.New("instance_not_found")
	ErrProcess     = errors.New("process_protection_failed")
	ErrProfile     = errors.New("apparmor_verification_failed")
	ErrMapping     = errors.New("user_mapping_verification_failed")
	ErrCgroup      = errors.New("cgroup_verification_failed")
	ErrNamespace   = errors.New("network_namespace_verification_failed")
	ErrReadiness   = errors.New("application_readiness_failed")
	ErrArtifact    = errors.New("artifact_rejected")
)

type Policy struct {
	SchemaVersion    int               `json:"schema_version"`
	Recipes          map[string]string `json:"recipes"` // Local immutable OCI image IDs.
	MaxInstances     int               `json:"max_instances"`
	MemoryBudgetMiB  int               `json:"memory_budget_mib"` // Aggregate workloads budget.
	BuilderArtifacts bool              `json:"builder_artifacts"`
}

func privateFile(path string, owner uint32) ([]byte, error) {
	return privateRead(path, owner, 65536)
}
func privateRead(path string, owner uint32, limit int64) ([]byte, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	meta, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || info.Mode().Perm()&0022 != 0 || meta.Uid != owner || meta.Nlink != 1 || info.Size() > limit {
		return nil, errors.New("unsafe_private_file")
	}
	return io.ReadAll(io.LimitReader(file, limit+1))
}

func SecureDir(path string, owner uint32) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return ErrInvalid
	}
	// Every ancestor must be root/owner-controlled; /tmp is intentionally not
	// accepted in the installed runtime. Tests use directories under an owned base.
	for current := path; ; current = filepath.Dir(current) {
		info, err := os.Lstat(current)
		if err != nil {
			return err
		}
		meta, ok := info.Sys().(*syscall.Stat_t)
		if !ok || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0022 != 0 ||
			(meta.Uid != owner && meta.Uid != 0) {
			return errors.New("unsafe_directory")
		}
		if current == "/" {
			break
		}
	}
	return nil
}

func LoadPolicy(path string) (Policy, error) {
	var policy Policy
	if err := SecureDir(filepath.Dir(path), 0); err != nil {
		return policy, err
	}
	data, err := privateFile(path, 0)
	if err != nil {
		return policy, err
	}
	if err = decodeStrict(data, &policy); err != nil {
		return policy, err
	}
	if policy.SchemaVersion != 1 || (len(policy.Recipes) < 1 && !policy.BuilderArtifacts) || len(policy.Recipes) > 32 ||
		policy.MaxInstances < 1 || policy.MaxInstances > 16 ||
		(policy.MemoryBudgetMiB != 0 && policy.MemoryBudgetMiB < 512) || policy.MemoryBudgetMiB > 1<<20 {
		return policy, ErrInvalid
	}
	for name, image := range policy.Recipes {
		if !recipePattern.MatchString(name) || !imagePattern.MatchString(image) {
			return policy, ErrInvalid
		}
	}
	return policy, nil
}

// Reject duplicate JSON object keys as well as unknown fields and trailing JSON.
func decodeStrict(data []byte, value any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	var walk func() error
	walk = func() error {
		token, err := d.Token()
		if err != nil {
			return err
		}
		switch token {
		case json.Delim('{'):
			seen := map[string]bool{}
			for d.More() {
				key, err := d.Token()
				name, ok := key.(string)
				if err != nil || !ok || seen[name] {
					return ErrInvalid
				}
				seen[name] = true
				if err = walk(); err != nil {
					return err
				}
			}
			_, err = d.Token()
			return err
		case json.Delim('['):
			for d.More() {
				if err := walk(); err != nil {
					return err
				}
			}
			_, err = d.Token()
			return err
		}
		return nil
	}
	if walk() != nil {
		return ErrInvalid
	}
	if _, err := d.Token(); err != io.EOF {
		return ErrInvalid
	}
	d = json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(value); err != nil {
		return ErrInvalid
	}
	return nil
}

type Spec struct {
	AppID      string `json:"app_id"`
	Recipe     string `json:"recipe"`
	Priority   string `json:"priority"`
	Artifact   string `json:"artifact,omitempty"`
	SourceHash string `json:"source_hash,omitempty"`
}

func (s Spec) Validate() error {
	validSource := recipePattern.MatchString(s.Recipe) && s.Artifact == "" && s.SourceHash == ""
	if s.Recipe == "" && digestPattern.MatchString(s.Artifact) && digestPattern.MatchString(s.SourceHash) {
		validSource = true
	}
	if !IDPattern.MatchString(s.AppID) || !validSource ||
		(s.Priority != "high" && s.Priority != "normal" && s.Priority != "low") {
		return ErrInvalid
	}
	return nil
}

func ContainerName(id string) string { return "shinyd-" + id }
func Slice(spec Spec) string {
	return fmt.Sprintf("shinyd-workloads-interactive-%s-app%s.slice", spec.Priority,
		regexp.MustCompile("-").ReplaceAllString(spec.AppID, ""))
}
func Target(id string) string { return "unix:/run/shinyd-runtime/apps/" + id + ".sock" }
