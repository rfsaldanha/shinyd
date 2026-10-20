package executor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

// DialNamespace creates the TCP socket in a pinned network namespace. It never
// resolves DNS or uses an app-supplied address. The OS thread is restored before
// polling or handing the socket to Go's network poller.
func DialNamespace(ctx context.Context, namespace *os.File) (net.Conn, error) {
	// A cancelled caller may return before the locked goroutine finishes.
	// Give that goroutine its own reference, independent of Relay.Close.
	pinned, err := unix.FcntlInt(namespace.Fd(), unix.F_DUPFD_CLOEXEC, 0)
	if err != nil {
		return nil, ErrUnavailable
	}
	type result struct {
		conn net.Conn
		err  error
	}
	done := make(chan result)
	go func() {
		defer unix.Close(pinned)
		runtime.LockOSThread()
		restored := true
		defer func() {
			if restored {
				runtime.UnlockOSThread()
			}
		}()
		send := func(conn net.Conn, err error) {
			select {
			case done <- result{conn, err}:
			case <-ctx.Done():
				if conn != nil {
					conn.Close()
				}
			}
		}
		original, err := unix.Open("/proc/thread-self/ns/net", unix.O_RDONLY|unix.O_CLOEXEC, 0)
		if err != nil {
			send(nil, ErrUnavailable)
			return
		}
		defer unix.Close(original)
		if unix.Setns(pinned, unix.CLONE_NEWNET) != nil {
			send(nil, ErrUnavailable)
			return
		}
		restored = false
		fd, socketErr := unix.Socket(unix.AF_INET, unix.SOCK_STREAM|unix.SOCK_CLOEXEC|unix.SOCK_NONBLOCK, 0)
		// On restoration failure the locked goroutine exits without unlocking.
		// Go terminates that OS thread; it cannot reenter the scheduler in the
		// container namespace. No socket is handed to another goroutine.
		if unix.Setns(original, unix.CLONE_NEWNET) != nil {
			if fd >= 0 {
				unix.Close(fd)
			}
			send(nil, ErrUnavailable)
			return
		}
		restored = true
		if socketErr != nil {
			send(nil, ErrUnavailable)
			return
		}
		file := os.NewFile(uintptr(fd), "container-tcp")
		defer file.Close()
		err = unix.Connect(fd, &unix.SockaddrInet4{Port: 3838, Addr: [4]byte{127, 0, 0, 1}})
		if err != nil && !errors.Is(err, unix.EINPROGRESS) {
			send(nil, ErrUnavailable)
			return
		}
		if err != nil {
			deadline := time.Now().Add(time.Second)
			if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
				deadline = d
			}
			for {
				if ctx.Err() != nil || time.Now().After(deadline) {
					send(nil, ErrUnavailable)
					return
				}
				polls := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLOUT}}
				n, pollErr := unix.Poll(polls, 50)
				if pollErr != nil && !errors.Is(pollErr, unix.EINTR) {
					send(nil, ErrUnavailable)
					return
				}
				if n > 0 {
					break
				}
			}
			code, e := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_ERROR)
			if e != nil || code != 0 {
				send(nil, ErrUnavailable)
				return
			}
		}
		conn, err := net.FileConn(file)
		send(conn, err)
	}()
	select {
	case value := <-done:
		return value.conn, value.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

type Relay struct {
	listener    *net.UnixListener
	namespace   *os.File
	cancel      context.CancelFunc
	mu          sync.Mutex
	connections map[net.Conn]bool
	closed      bool
	wg          sync.WaitGroup
}

func NewRelay(path string, namespace *os.File, gid int, slots chan struct{}) (*Relay, error) {
	if info, err := os.Lstat(path); err == nil {
		var stat unix.Stat_t
		if info.Mode()&os.ModeSocket == 0 || unix.Lstat(path, &stat) != nil || stat.Uid != 0 {
			return nil, ErrOwnership
		}
		if err = os.Remove(path); err != nil {
			return nil, err
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		return nil, err
	}
	if err = os.Chown(path, 0, gid); err == nil {
		err = os.Chmod(path, 0660)
	}
	if err != nil {
		listener.Close()
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	relay := &Relay{listener: listener, namespace: namespace, cancel: cancel, connections: map[net.Conn]bool{}}
	relay.wg.Add(1)
	go func() {
		defer relay.wg.Done()
		for {
			client, err := listener.AcceptUnix()
			if err != nil {
				return
			}
			select {
			case slots <- struct{}{}:
			default:
				client.Close()
				continue
			}
			if !relay.track(client) {
				<-slots
				continue
			}
			relay.wg.Add(1)
			go func() {
				defer relay.wg.Done()
				defer func() { <-slots }()
				defer relay.untrack(client)
				upstream, err := DialNamespace(ctx, namespace)
				if err != nil {
					return
				}
				if !relay.track(upstream) {
					return
				}
				defer relay.untrack(upstream)
				done := make(chan struct{}, 2)
				copyHalf := func(dst, src net.Conn) {
					_, _ = io.Copy(dst, src)
					if half, ok := dst.(interface{ CloseWrite() error }); ok {
						_ = half.CloseWrite()
					}
					done <- struct{}{}
				}
				go copyHalf(upstream, client)
				go copyHalf(client, upstream)
				<-done
				<-done
			}()
		}
	}()
	return relay, nil
}
func (r *Relay) track(conn net.Conn) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		conn.Close()
		return false
	}
	r.connections[conn] = true
	return true
}
func (r *Relay) untrack(conn net.Conn) {
	conn.Close()
	r.mu.Lock()
	delete(r.connections, conn)
	r.mu.Unlock()
}
func (r *Relay) Close() {
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		return
	}
	r.closed = true
	r.cancel()
	r.listener.Close()
	for conn := range r.connections {
		conn.Close()
	}
	r.mu.Unlock()
	r.wg.Wait()
	r.namespace.Close()
}

// PinNamespace validates the running instance before opening its namespace.
// The descriptor pins the namespace, so later PID reuse cannot redirect traffic.
func PinNamespace(value *Inspection, record Record) (*os.File, error) {
	if value == nil || !value.State.Running || value.State.Pid <= 1 {
		return nil, ErrProcess
	}
	prefix := fmt.Sprintf("/proc/%d", value.State.Pid)
	status, err := os.ReadFile(prefix + "/status")
	if err != nil {
		return nil, ErrProcess
	}
	required := map[string]string{"NoNewPrivs": "1", "Seccomp": "2", "CapEff": "0000000000000000"}
	for _, line := range strings.Split(string(status), "\n") {
		key, val, ok := strings.Cut(line, ":")
		val = strings.TrimSpace(val)
		if ok {
			if expected, exists := required[key]; exists {
				if val != expected {
					return nil, ErrProcess
				}
				delete(required, key)
			}
		}
	}
	if len(required) != 0 {
		return nil, ErrProcess
	}
	// Select AppArmor explicitly on kernels with multiple security modules.
	profile, err := os.ReadFile(prefix + "/attr/apparmor/current")
	if err != nil || !strings.Contains(string(profile), "(enforce)") {
		return nil, ErrProfile
	}
	mapping, err := os.ReadFile(prefix + "/uid_map")
	if err != nil {
		return nil, ErrMapping
	}
	var inside, outside, count uint64
	if _, err = fmt.Sscanf(string(mapping), "%d %d %d", &inside, &outside, &count); err != nil || inside != 0 || outside == 0 || count < 65536 {
		return nil, ErrMapping
	}
	cgroup, err := os.ReadFile(prefix + "/cgroup")
	if err != nil || !strings.Contains(string(cgroup), "/"+Slice(record.Spec)+"/") ||
		len(value.ID) != 64 || !strings.Contains(string(cgroup), "/libpod-"+value.ID+".scope") {
		return nil, ErrCgroup
	}
	namespace, err := os.Open(prefix + "/ns/net")
	if err != nil {
		return nil, ErrNamespace
	}
	var target, host unix.Stat_t
	if unix.Fstat(int(namespace.Fd()), &target) != nil || unix.Stat("/proc/1/ns/net", &host) != nil || target.Ino == host.Ino {
		namespace.Close()
		return nil, ErrNamespace
	}
	after, err := os.ReadFile(prefix + "/cgroup")
	if err != nil || string(after) != string(cgroup) {
		namespace.Close()
		return nil, ErrCgroup
	}
	return namespace, nil
}
