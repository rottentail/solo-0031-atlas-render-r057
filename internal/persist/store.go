package persist

import (
	"atlas-render/internal/model"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

type Store struct {
	mu    sync.RWMutex
	path  string
	state model.State
	lock  *os.File
	write func(string, []byte) error
}

func Open(path string) (*Store, error) {
	if e := os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return nil, e
	}
	lock, e := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		lock.Close()
		return nil, fmt.Errorf("state is already opened by another process: %w", e)
	}
	s := &Store{path: path, state: model.NewState(), lock: lock, write: atomicWrite}
	data, e := os.ReadFile(path)
	if e != nil && !os.IsNotExist(e) {
		s.Close()
		return nil, e
	}
	if e == nil {
		if e = json.Unmarshal(data, &s.state); e != nil {
			s.Close()
			return nil, fmt.Errorf("invalid persisted JSON: %w", e)
		}
		if e = Validate(s.state); e != nil {
			s.Close()
			return nil, e
		}
	}
	return s, nil
}
func (s *Store) Close() error {
	if s.lock == nil {
		return nil
	}
	_ = syscall.Flock(int(s.lock.Fd()), syscall.LOCK_UN)
	e := s.lock.Close()
	s.lock = nil
	return e
}

// View 在读锁内同步执行只读检查，不复制状态。fn 不得修改状态、
// 不得保存引用在锁外使用；需要带出数据时在 fn 内显式复制。
// 过载期间状态可能很大，热路径（状态、事件、任务查询、执行器领取）
// 必须使用 View 而不是全量克隆，保证健康与取消查询有有界延迟。
func (s *Store) View(fn func(model.State)) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	fn(s.state)
}
func (s *Store) Update(fn func(*model.State) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	next := model.Clone(s.state)
	if e := fn(&next); e != nil {
		return e
	}
	if e := Validate(next); e != nil {
		return e
	}
	b, e := json.Marshal(next)
	if e != nil {
		return e
	}
	if e = s.write(s.path, b); e != nil {
		return fmt.Errorf("persist state: %w", e)
	}
	s.state = next
	return nil
}
func atomicWrite(path string, data []byte) error {
	f, e := os.CreateTemp(filepath.Dir(path), ".atlas-write-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e == nil {
		_, e = f.Write(data)
	}
	if e == nil {
		e = f.Sync()
	}
	closeErr := f.Close()
	if e == nil {
		e = closeErr
	}
	if e != nil {
		return e
	}
	return os.Rename(name, path)
}
