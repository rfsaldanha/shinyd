package executor

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

type Inspection struct {
	ID              string `json:"Id"`
	Image           string `json:"Image"`
	AppArmorProfile string `json:"AppArmorProfile"`
	State           struct {
		Running   bool `json:"Running"`
		Pid       int  `json:"Pid"`
		OOMKilled bool `json:"OOMKilled"`
	} `json:"State"`
	Config struct {
		Labels map[string]string `json:"Labels"`
		User   string            `json:"User"`
	} `json:"Config"`
	HostConfig struct {
		NetworkMode    string `json:"NetworkMode"`
		ReadonlyRootfs bool   `json:"ReadonlyRootfs"`
	} `json:"HostConfig"`
	Mounts []struct {
		Type, Source, Destination string
		RW                        bool
	} `json:"Mounts"`
}
type Backend interface {
	Inspect(context.Context, Record) (*Inspection, error)
	Start(context.Context, Record, map[string]string) error
	Stop(context.Context, Record) error
	Remove(context.Context, Record) error
}
type Runner func(context.Context, string, ...string) ([]byte, error)
type Podman struct {
	Run            Runner
	RunEnvironment func(context.Context, map[string]string, ...string) ([]byte, error)
}

type cappedOutput struct{ bytes.Buffer }

func (b *cappedOutput) Write(p []byte) (int, error) {
	if b.Len()+len(p) > 256*1024 {
		return 0, errors.New("output_limit")
	}
	return b.Buffer.Write(p)
}
func RunCommand(ctx context.Context, binary string, args ...string) ([]byte, error) {
	return runCommand(ctx, binary, nil, args...)
}
func runCommand(ctx context.Context, binary string, files []*os.File, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if binary == "/usr/bin/podman" {
		// Podman can send image labels straight to journald, independently
		// of stdout/stderr. Keep that metadata outside the control audit.
		args = append([]string{"--events-backend=none"}, args...)
	}
	cmd := exec.CommandContext(ctx, binary, args...)
	cmd.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root", "LANG=C.UTF-8"}
	cmd.ExtraFiles = files
	var out cappedOutput
	cmd.Stdout = &out
	// Never return Podman's stderr, container output, env or arguments to callers.
	if err := cmd.Run(); err != nil {
		return nil, ErrUnavailable
	}
	return out.Bytes(), nil
}
func signature(r Record) string {
	data, _ := json.Marshal(struct {
		Spec  Spec
		Image string
	}{r.Spec, r.Image})
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
func (p Podman) Inspect(ctx context.Context, r Record) (*Inspection, error) {
	data, err := p.Run(ctx, "/usr/bin/podman", "container", "inspect", ContainerName(r.ID))
	if err != nil {
		// Distinguish absence from a broken runtime; never assume an error means
		// it is safe to create/replace a similarly named host resource.
		list, listErr := p.Run(ctx, "/usr/bin/podman", "ps", "--all", "--format", "json", "--filter", "name=^"+ContainerName(r.ID)+"$")
		if listErr != nil {
			return nil, ErrUnavailable
		}
		var rows []json.RawMessage
		if json.Unmarshal(list, &rows) != nil {
			return nil, ErrUnavailable
		}
		if len(rows) == 0 {
			return nil, nil
		}
		return nil, ErrUnavailable
	}
	var rows []Inspection
	if json.Unmarshal(data, &rows) != nil || len(rows) != 1 {
		return nil, ErrUnavailable
	}
	value := rows[0]
	labels := value.Config.Labels
	if labels["org.shinyd.managed"] != "executor-v1" || labels["org.shinyd.instance"] != r.ID ||
		labels["org.shinyd.spec"] != signature(r) || strings.TrimPrefix(value.Image, "sha256:") != strings.TrimPrefix(r.Image, "sha256:") ||
		value.Config.User != "65534:65534" || value.HostConfig.NetworkMode != "none" || !value.HostConfig.ReadonlyRootfs {
		return nil, ErrOwnership
	}
	if r.Spec.DataMode == "persistent" {
		found := false
		for _, mount := range value.Mounts {
			if mount.Destination == "/data" && mount.Source == dataPath(r.Spec.AppID) && mount.Type == "bind" && mount.RW {
				found = true
			}
		}
		if !found {
			return nil, ErrOwnership
		}
	}
	return &value, nil
}
func (p Podman) Start(ctx context.Context, r Record, environment map[string]string) error {
	if validateEnvironment(r.Spec, environment) != nil {
		return ErrInvalid
	}
	existing, err := p.Inspect(ctx, r)
	if err != nil {
		return err
	}
	if existing != nil {
		if existing.State.Running {
			return nil
		}
		// Inherited descriptors only exist during the initial launch. Artifact
		// instances are once-only and need a fresh publication after failure.
		if r.Spec.ConfigRevision != 0 {
			return ErrConflict
		}
		_, err = p.Run(ctx, "/usr/bin/podman", "start", ContainerName(r.ID))
		return err
	}
	args := []string{"run", "--detach", "--pull=never", "--name", ContainerName(r.ID),
		"--label=org.shinyd.managed=executor-v1", "--label=org.shinyd.instance=" + r.ID, "--label=org.shinyd.spec=" + signature(r),
		"--network=none", "--userns=auto:size=65536", "--user=65534:65534",
		"--read-only", "--read-only-tmpfs=false", "--image-volume=ignore", "--cap-drop=all",
		"--security-opt=no-new-privileges", "--ulimit=core=0:0", "--pids-limit=128", "--cgroups=enabled", "--cgroupns=private",
		"--cgroup-parent=" + Slice(r.Spec), "--restart=no",
		"--log-driver=k8s-file", "--log-opt=max-size=1mb",
		"--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777", "--env=HOME=/tmp",
		"--env=SHINYD_INSTANCE_ID=" + r.ID, "--env=PORT=3838"}
	if r.Spec.DataMode != "" {
		args = append([]string{"--runtime=/usr/bin/crun"}, args...)
		args = append(args, "--env=SHINYD_DATA_DIR=/data")
		if r.Spec.DataMode == "scratch" {
			args = append(args, "--tmpfs=/data:rw,nosuid,nodev,noexec,size=64m,mode=1777")
		} else {
			if err := prepareData(DataRoot, r.Spec.AppID, 0, 65534); err != nil {
				return ErrDataStorage
			}
			args = append(args, "--volume="+dataPath(r.Spec.AppID)+":/data:rw,nosuid,nodev,noexec,idmap,rprivate")
		}
	}
	args = append(args, r.Image)
	if r.Spec.ConfigRevision != 0 {
		if p.RunEnvironment == nil {
			return ErrUnavailable
		}
		args = args[:len(args)-1]
		if r.Spec.DataMode == "" {
			args = append([]string{"--runtime=/usr/bin/crun"}, args...)
		}
		args = append(args, "--preserve-fds=1", "--entrypoint=Rscript", r.Image, "--vanilla", "-e", environmentLaunch)
		_, err = p.RunEnvironment(ctx, environment, args...)
	} else {
		_, err = p.Run(ctx, "/usr/bin/podman", args...)
	}
	return err
}
func (p Podman) Stop(ctx context.Context, r Record) error {
	value, err := p.Inspect(ctx, r)
	if err != nil || value == nil {
		return err
	}
	if !value.State.Running {
		return nil
	}
	_, err = p.Run(ctx, "/usr/bin/podman", "stop", "--time=5", ContainerName(r.ID))
	return err
}
func (p Podman) Remove(ctx context.Context, r Record) error {
	value, err := p.Inspect(ctx, r)
	if err != nil || value == nil {
		return err
	}
	_, err = p.Run(ctx, "/usr/bin/podman", "rm", "--force", ContainerName(r.ID))
	return err
}

// ConfigureBudget installs an aggregate ceiling, not per-app CPU/RAM quotas.
// Slice ancestors and group weights are supplied by the package.
func ConfigureBudget(ctx context.Context, run Runner, policy Policy) error {
	info, err := run(ctx, "/usr/bin/podman", "info", "--format", "json")
	if err != nil {
		return err
	}
	var data struct {
		Host struct {
			CgroupVersion     string   `json:"cgroupVersion"`
			CgroupManager     string   `json:"cgroupManager"`
			CgroupControllers []string `json:"cgroupControllers"`
			Security          struct {
				ApparmorEnabled bool `json:"apparmorEnabled"`
				Rootless        bool `json:"rootless"`
			} `json:"security"`
		} `json:"host"`
	}
	if json.Unmarshal(info, &data) != nil || data.Host.CgroupVersion != "v2" ||
		data.Host.CgroupManager != "systemd" || !data.Host.Security.ApparmorEnabled || data.Host.Security.Rootless {
		return ErrUnavailable
	}
	for _, controller := range []string{"cpu", "memory", "pids"} {
		if !strings.Contains(" "+strings.Join(data.Host.CgroupControllers, " ")+" ", " "+controller+" ") {
			return ErrUnavailable
		}
	}
	total, available, err := Memory()
	reserve := max(uint64(1<<30), total*15/100)
	budget := uint64(policy.MemoryBudgetMiB) << 20
	if err != nil || total <= reserve || budget > total-reserve || available < reserve {
		return ErrAdmission
	}
	_, err = run(ctx, "/usr/bin/systemctl", "set-property", "--runtime", "shinyd-workloads.slice",
		"MemoryMax="+strconv.FormatUint(budget, 10), "MemoryHigh="+strconv.FormatUint(budget*9/10, 10),
		"MemorySwapMax=0", "TasksMax=4096")
	return err
}
func Memory() (total, available uint64, err error) {
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0, 0, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "MemTotal:") {
			fmt.Sscanf(line, "MemTotal: %d kB", &total)
		}
		if strings.HasPrefix(line, "MemAvailable:") {
			fmt.Sscanf(line, "MemAvailable: %d kB", &available)
		}
	}
	if total == 0 || available == 0 {
		return 0, 0, ErrUnavailable
	}
	return total << 10, available << 10, nil
}
