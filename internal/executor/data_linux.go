package executor

import (
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// Not an API parameter. Neither publisher nor control chooses host paths.
const DataRoot = "/var/lib/shinyd-data"

func dataPath(appID string) string { return filepath.Join(DataRoot, appID, "files") }

// The root and app parent are private to the executor. Only files/ is mounted.
// idmap translates on-disk nobody to each container's distinct subordinate UID.
// Never walk/chown publisher-controlled descendants or follow their symlinks.
func prepareData(root, appID string, owner, dataOwner int) error {
	if !IDPattern.MatchString(appID) {
		return ErrInvalid
	}
	fd, err := unix.Open(root, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return ErrDataStorage
	}
	defer unix.Close(fd)
	var stat unix.Stat_t
	if unix.Fstat(fd, &stat) != nil || stat.Uid != uint32(owner) || stat.Mode&0077 != 0 {
		return ErrDataStorage
	}
	var space unix.Statfs_t
	if unix.Fstatfs(fd, &space) != nil || space.Bavail*uint64(space.Bsize) < 1<<30 {
		return ErrDataStorage
	}
	app, err := dataDirectory(fd, appID, owner, true)
	if err != nil {
		return ErrDataStorage
	}
	defer unix.Close(app)
	files, err := dataDirectory(app, "files", dataOwner, false)
	if err != nil {
		return ErrDataStorage
	}
	defer unix.Close(files)
	// Directory creation/ownership survive a daemon crash before container launch.
	if unix.Fsync(files) != nil || unix.Fsync(app) != nil || unix.Fsync(fd) != nil {
		return ErrDataStorage
	}
	return nil
}

func dataDirectory(parent int, name string, owner int, private bool) (int, error) {
	err := unix.Mkdirat(parent, name, 0700)
	created := err == nil
	if err != nil && !os.IsExist(err) {
		return -1, err
	}
	fd, err := unix.Openat(parent, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, err
	}
	if created {
		err = unix.Fchown(fd, owner, owner)
	}
	var stat unix.Stat_t
	if err != nil || unix.Fstat(fd, &stat) != nil || stat.Uid != uint32(owner) || stat.Gid != uint32(owner) || (private && stat.Mode&0077 != 0) {
		unix.Close(fd)
		return -1, ErrDataStorage
	}
	return fd, nil
}
