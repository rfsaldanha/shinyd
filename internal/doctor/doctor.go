// Package doctor inspects a host without modifying it or executing workloads.
package doctor

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Check struct {
	Code     string `json:"code"`
	Status   string `json:"status"` // pass, fail, unknown, warning
	Required bool   `json:"required"`
	Detail   string `json:"detail"`
}

type Report struct {
	SchemaVersion int       `json:"schema_version"`
	ObservedAt    time.Time `json:"observed_at"`
	OS            string    `json:"os"`
	Architecture  string    `json:"architecture"`
	Checks        []Check   `json:"checks"`
	Ready         bool      `json:"prerequisites_ready"`
	Notice        string    `json:"notice"`
}

// Probe allows tests to simulate missing controllers and permission failures.
type Probe struct {
	OS, Arch string
	ReadFile func(string) ([]byte, error)
	LookPath func(string) (string, error)
	DiskFree func(string) (uint64, error)
}

func Local() Probe {
	return Probe{
		OS: runtime.GOOS, Arch: runtime.GOARCH, ReadFile: os.ReadFile,
		LookPath: exec.LookPath,
		DiskFree: func(path string) (uint64, error) {
			var s syscall.Statfs_t
			if err := syscall.Statfs(path, &s); err != nil {
				return 0, err
			}
			return s.Bavail * uint64(s.Bsize), nil
		},
	}
}

func Inspect(p Probe, dataPath string) Report {
	r := Report{
		SchemaVersion: 1, ObservedAt: time.Now().UTC(), OS: p.OS, Architecture: p.Arch,
		Ready: true, Notice: "Pré-requisitos observados; não homologa containers, delegação, segurança, rede ou capacidade.",
	}
	add := func(code, status string, required bool, detail string) {
		r.Checks = append(r.Checks, Check{code, status, required, detail})
		if required && status != "pass" {
			r.Ready = false
		}
	}
	condition := func(code string, ok bool, detail string) {
		status := "pass"
		if !ok {
			status = "fail"
		}
		add(code, status, true, detail)
	}
	read := func(code, path string, required bool) (string, bool) {
		b, err := p.ReadFile(path)
		if err == nil {
			return strings.TrimSpace(string(b)), true
		}
		status, detail := "unknown", "Não foi possível verificar: "+err.Error()
		if errors.Is(err, fs.ErrNotExist) {
			status, detail = "fail", "Recurso ausente: "+path
		}
		if !required && status == "fail" {
			status = "warning"
		}
		add(code, status, required, detail)
		return "", false
	}
	condition("platform.linux", p.OS == "linux", "Sistema: "+p.OS)
	condition("platform.arch", p.Arch == "amd64", "Arquitetura de referência: amd64; observada: "+p.Arch)
	if v, ok := read("platform.distribution", "/etc/os-release", true); ok {
		fields := map[string]string{}
		for _, line := range strings.Split(v, "\n") {
			k, value, found := strings.Cut(line, "=")
			if found {
				fields[k] = strings.Trim(value, "\"'")
			}
		}
		condition("platform.distribution", fields["ID"] == "ubuntu" && fields["VERSION_ID"] == "24.04", "Referência Ubuntu 24.04; observado: "+fields["PRETTY_NAME"])
	}
	if v, ok := read("systemd.pid1", "/proc/1/comm", true); ok {
		condition("systemd.pid1", v == "systemd", "PID 1: "+v)
	}
	if v, ok := read("cgroup.v2", "/sys/fs/cgroup/cgroup.controllers", true); ok {
		add("cgroup.v2", "pass", true, "Interface unificada encontrada")
		controllers := strings.Fields(v)
		for _, name := range []string{"cpu", "memory", "pids", "io"} {
			present := false
			for _, controller := range controllers {
				present = present || controller == name
			}
			if name == "io" {
				status := "pass"
				if !present {
					status = "warning"
				}
				add("cgroup.io", status, false, "Controle de I/O opcional; depende também do dispositivo")
			} else {
				condition("cgroup."+name, present, "Controlador disponível na raiz: "+name)
			}
		}
	}
	for _, resource := range []string{"cpu", "memory", "io"} {
		if v, ok := read("psi."+resource, "/proc/pressure/"+resource, true); ok {
			condition("psi."+resource, strings.Contains(v, "some avg10="), "Interface PSI legível")
		}
	}
	if v, ok := read("namespaces.user", "/proc/sys/user/max_user_namespaces", true); ok {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			add("namespaces.user", "unknown", true, "Valor inválido do kernel")
		} else {
			condition("namespaces.user", n > 0, "Limite configurado: "+v+"; criação efetiva ainda exige teste de runtime")
		}
	}
	if v, ok := read("security.apparmor", "/sys/module/apparmor/parameters/enabled", true); ok {
		condition("security.apparmor", v == "Y", "AppArmor habilitado: "+v)
	}
	if v, ok := read("capacity.memory", "/proc/meminfo", true); ok {
		var total uint64
		for _, line := range strings.Split(v, "\n") {
			if strings.HasPrefix(line, "MemTotal:") {
				_, _ = fmt.Sscanf(line, "MemTotal: %d kB", &total)
			}
		}
		if total == 0 {
			add("capacity.memory", "unknown", true, "MemTotal ausente ou inválido")
		} else {
			add("capacity.memory", "pass", true, fmt.Sprintf("%d MiB de RAM; capacidade de apps não medida", total/1024))
		}
	}
	if free, err := p.DiskFree(dataPath); err != nil {
		add("capacity.disk", "unknown", true, "Não foi possível inspecionar "+dataPath+": "+err.Error())
	} else {
		status := "pass"
		if free == 0 {
			status = "fail"
		}
		add("capacity.disk", status, true, fmt.Sprintf("%d MiB livres para o usuário em %s; não estima capacidade", free/(1024*1024), dataPath))
	}
	for _, binary := range []string{"podman", "systemctl"} {
		path, err := p.LookPath(binary)
		status := "pass"
		detail := path
		if err != nil {
			status, detail = "warning", binary+" não encontrado no PATH; necessário para execução"
		}
		add("tool."+binary, status, false, detail)
	}
	return r
}

// ExitCode prioritizes known failures (1) over inconclusive checks (2).
func (r Report) ExitCode() int {
	code := 0
	for _, c := range r.Checks {
		if c.Required && c.Status == "fail" {
			return 1
		}
		if c.Required && c.Status == "unknown" {
			code = 2
		}
	}
	return code
}
