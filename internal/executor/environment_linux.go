package executor

import (
	"context"
	"encoding/json"
	"os"
	"regexp"
	"strings"
	"unicode/utf8"

	"golang.org/x/sys/unix"
)

const maxEnvironmentRequest = 512 * 1024

var environmentName = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,127}$`)

func validateEnvironment(spec Spec, values map[string]string) error {
	if spec.ConfigRevision == 0 {
		if values != nil {
			return ErrInvalid
		}
		return nil
	}
	if values == nil || len(values) > 64 {
		return ErrInvalid
	}
	total := 0
	for name, value := range values {
		if !environmentName.MatchString(name) || !utf8.ValidString(value) || strings.ContainsRune(value, 0) || len(value) > 16384 {
			return ErrInvalid
		}
		for _, prefix := range []string{"SHINYD_", "LD_", "DYLD_", "R_", "RENV_"} {
			if strings.HasPrefix(name, prefix) {
				return ErrInvalid
			}
		}
		switch name {
		case "PATH", "HOME", "USER", "SHELL", "TMPDIR", "PYTHONPATH", "BASH_ENV", "ENV", "NODE_OPTIONS", "IFS", "PORT", "RHOME":
			return ErrInvalid
		}
		total += len(name) + len(value)
	}
	if total > 65536 {
		return ErrInvalid
	}
	return nil
}

// The sole extra FD contains bounded JSON, never a host file/socket/directory.
// Seals prevent writes even if the app inherits a writable description.
func environmentFile(values map[string]string) (*os.File, error) {
	data, err := json.Marshal(values)
	if err != nil {
		return nil, ErrInvalid
	}
	defer clear(data)
	fd, err := unix.MemfdCreate("shinyd-environment", unix.MFD_CLOEXEC|unix.MFD_ALLOW_SEALING)
	if err != nil {
		return nil, ErrUnavailable
	}
	file := os.NewFile(uintptr(fd), "shinyd-environment")
	ok := false
	defer func() {
		if !ok {
			file.Close()
		}
	}()
	if _, err = file.Write(data); err != nil {
		return nil, ErrUnavailable
	}
	if _, err = file.Seek(0, 0); err != nil {
		return nil, ErrUnavailable
	}
	// The mapped, non-root app UID must be able to reopen its inherited FD.
	if file.Chmod(0444) != nil {
		return nil, ErrUnavailable
	}
	if _, err = unix.FcntlInt(file.Fd(), unix.F_ADD_SEALS, unix.F_SEAL_WRITE|unix.F_SEAL_GROW|unix.F_SEAL_SHRINK|unix.F_SEAL_SEAL); err != nil {
		return nil, ErrUnavailable
	}
	ok = true
	return file, nil
}

func RunWithEnvironment(ctx context.Context, values map[string]string, args ...string) ([]byte, error) {
	file, err := environmentFile(values)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return runCommand(ctx, "/usr/bin/podman", []*os.File{file}, args...)
}

// Fixed code, never interpolated publisher input. JSON strings (including
// newlines, quotes and UTF-8) become environment values, not R source code.
// The existing approved image contract supplies jsonlite and launch.R.
const environmentLaunch = `.libPaths(c("/opt/shinyd/library", .Library), include.site=FALSE); local({
tryCatch({v <- jsonlite::fromJSON("/proc/self/fd/3", simplifyVector=FALSE); if(length(v)) do.call(Sys.setenv, v)},
error=function(e) {cat("Environment initialization failed\n", file=stderr()); quit(status=78, save="no")})
}); source("/opt/shinyd/launch.R")`
