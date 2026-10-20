package executor

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

type Record struct {
	SchemaVersion int         `json:"schema_version"`
	ID            string      `json:"id"`
	Spec          Spec        `json:"spec"`
	Image         string      `json:"image"`
	Desired       string      `json:"desired"`
	Attempts      []time.Time `json:"attempts"`
}
type Store struct {
	dir   string
	lock  *os.File
	owner uint32
}

func OpenStore(dir string, owner uint32) (*Store, error) {
	if err := SecureDir(dir, owner); err != nil {
		return nil, err
	}
	fd, err := syscall.Open(filepath.Join(dir, ".lock"), syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0600)
	if err != nil {
		return nil, err
	}
	lock := os.NewFile(uintptr(fd), "lock")
	info, err := lock.Stat()
	if err != nil {
		lock.Close()
		return nil, err
	}
	meta, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.Mode().IsRegular() || meta.Uid != owner || meta.Nlink != 1 || info.Mode().Perm()&0077 != 0 {
		lock.Close()
		return nil, errors.New("unsafe_lock")
	}
	if err = syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, errors.New("runtime_already_running")
	}
	return &Store{dir, lock, owner}, nil
}
func (s *Store) Close() error { return s.lock.Close() }
func (s *Store) Load() (map[string]Record, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, err
	}
	result := map[string]Record{}
	for _, entry := range entries {
		if entry.Name() == ".lock" || strings.HasPrefix(entry.Name(), ".tmp-") {
			continue
		}
		id := strings.TrimSuffix(entry.Name(), ".json")
		if !IDPattern.MatchString(id) || entry.Name() != id+".json" || len(result) >= 256 {
			return nil, ErrInvalid
		}
		data, err := privateFile(filepath.Join(s.dir, entry.Name()), s.owner)
		if err != nil {
			return nil, err
		}
		var record Record
		if decodeStrict(data, &record) != nil || record.ID != id || record.SchemaVersion != 1 ||
			record.Spec.Validate() != nil || !imagePattern.MatchString(record.Image) ||
			(record.Desired != "running" && record.Desired != "stopped") || len(record.Attempts) > 3 {
			return nil, ErrInvalid
		}
		result[id] = record
	}
	return result, nil
}
func (s *Store) Save(record Record) error {
	if !IDPattern.MatchString(record.ID) {
		return ErrInvalid
	}
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(s.dir, ".tmp-")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(data); err == nil {
		err = file.Sync()
	}
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err = os.Rename(file.Name(), filepath.Join(s.dir, record.ID+".json")); err != nil {
		return err
	}
	return syncDir(s.dir)
}
func (s *Store) Remove(id string) error {
	if !IDPattern.MatchString(id) {
		return ErrInvalid
	}
	if err := os.Remove(filepath.Join(s.dir, id+".json")); err != nil && !os.IsNotExist(err) {
		return err
	}
	return syncDir(s.dir)
}
func syncDir(path string) error {
	d, err := os.Open(path)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
