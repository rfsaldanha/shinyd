package doctor

import (
	"encoding/json"
	"io/fs"
	"strings"
	"testing"
)

func fixture() Probe {
	files := map[string]string{
		"/etc/os-release": "ID=ubuntu\nVERSION_ID=\"24.04\"\nPRETTY_NAME=\"Ubuntu 24.04 LTS\"",
		"/proc/1/comm":    "systemd\n", "/sys/fs/cgroup/cgroup.controllers": "cpu memory pids io",
		"/proc/pressure/cpu": "some avg10=0.00", "/proc/pressure/memory": "some avg10=0.00",
		"/proc/pressure/io": "some avg10=0.00", "/proc/sys/user/max_user_namespaces": "100",
		"/sys/module/apparmor/parameters/enabled": "Y\n", "/proc/meminfo": "MemTotal: 8000000 kB\n",
	}
	return Probe{"linux", "amd64", func(path string) ([]byte, error) {
		v, ok := files[path]
		if !ok {
			return nil, fs.ErrNotExist
		}
		return []byte(v), nil
	}, func(s string) (string, error) { return "/usr/bin/" + s, nil },
		func(string) (uint64, error) { return 100 << 30, nil }}
}

func TestReadyReport(t *testing.T) {
	r := Inspect(fixture(), "/data")
	if !r.Ready || r.ExitCode() != 0 {
		t.Fatalf("unexpected report: %+v", r)
	}
	b, err := json.Marshal(r)
	if err != nil || !strings.Contains(string(b), `"schema_version":1`) {
		t.Fatal("missing stable JSON schema")
	}
}

func TestMissingControllerAndPermissionAreDifferent(t *testing.T) {
	for _, tc := range []struct {
		name    string
		content string
		err     error
		exit    int
		status  string
	}{
		{"no memory", "cpu pids", nil, 1, "fail"},
		{"permission", "", fs.ErrPermission, 2, "unknown"},
		{"v1 only", "", fs.ErrNotExist, 1, "fail"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := fixture()
			read := p.ReadFile
			p.ReadFile = func(path string) ([]byte, error) {
				if path == "/sys/fs/cgroup/cgroup.controllers" {
					return []byte(tc.content), tc.err
				}
				return read(path)
			}
			r := Inspect(p, "/data")
			if r.Ready || r.ExitCode() != tc.exit {
				t.Fatalf("unexpected report: %+v", r)
			}
			found := false
			for _, c := range r.Checks {
				found = found || (strings.HasPrefix(c.Code, "cgroup.") && c.Status == tc.status)
			}
			if !found {
				t.Fatal("expected diagnostic absent")
			}
		})
	}
}

func TestDiskFailureIsNotCapacitySuccess(t *testing.T) {
	p := fixture()
	p.DiskFree = func(string) (uint64, error) { return 0, fs.ErrPermission }
	if r := Inspect(p, "/root"); r.ExitCode() != 2 || r.Ready {
		t.Fatal("unreadable disk must be inconclusive")
	}
}

func TestOptionalIOAndRuntimeDoNotBlockInspection(t *testing.T) {
	p := fixture()
	read := p.ReadFile
	p.ReadFile = func(path string) ([]byte, error) {
		if path == "/sys/fs/cgroup/cgroup.controllers" {
			return []byte("cpu memory pids"), nil
		}
		return read(path)
	}
	p.LookPath = func(string) (string, error) { return "", fs.ErrNotExist }
	if r := Inspect(p, "/data"); r.ExitCode() != 0 || !r.Ready {
		t.Fatal("optional runtime availability must not claim a required failure")
	}
}
