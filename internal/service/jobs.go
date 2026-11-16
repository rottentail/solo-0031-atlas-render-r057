package service

import (
	"atlas-render/internal/capacity"
	"atlas-render/internal/model"
	"atlas-render/internal/raster"
	"atlas-render/internal/scene"
	"fmt"
	"time"
)

// admit 在持久事务内核对队列门禁：未结束承诺的数量与驻留字节估计
// 都必须留出余量。门禁拒绝不写入任何事实，幂等收据也不会被消耗。
func (s *Service) admit(st model.State, extra int64) error {
	depth, bytes := capacity.QueueUsage(st)
	if depth >= s.Limits.QueueDepth {
		return model.Busy("render queue is full")
	}
	if bytes+extra > s.Limits.QueueBytes {
		return model.Busy("render queue byte budget exhausted")
	}
	return nil
}

func (s *Service) Submit(snapshotID string, scale int, key string) (model.Job, error) {
	var out model.Job
	if !scene.ValidID(key) {
		return out, model.Invalid("idempotency key required (1..80 identifier characters)")
	}
	fingerprint := model.CanonicalDigest([]any{snapshotID, scale})
	e := s.Store.Update(func(st *model.State) error {
		if r, ok := st.Receipts[key]; ok {
			if r.Fingerprint != fingerprint {
				return model.Conflict("idempotency key used for different request")
			}
			out = st.Jobs[r.JobID]
			return nil
		}
		snap, ok := st.Snapshots[snapshotID]
		if !ok {
			return model.Missing("snapshot not found")
		}
		if snap.State != "active" {
			return model.Conflict("snapshot is revoked")
		}
		if _, e := raster.Compile(snap, scale, s.Limits); e != nil {
			return e
		}
		if e := s.admit(*st, capacity.Footprint(snap, scale)); e != nil {
			return e
		}
		now := time.Now().UTC()
		out = model.Job{ID: model.ID("job", st.Sequence+1), SnapshotID: snapshotID, State: "queued", Scale: scale, Attempt: 1, CreatedAt: now, UpdatedAt: now, Payload: snap}
		st.Jobs[out.ID] = out
		st.Receipts[key] = model.Receipt{Key: key, Fingerprint: fingerprint, JobID: out.ID}
		st.Emit("job.queued", out.ID)
		return nil
	})
	return out, e
}
func (s *Service) Cancel(id string) (model.Job, error) {
	var out model.Job
	e := s.Store.Update(func(st *model.State) error {
		j, ok := st.Jobs[id]
		if !ok {
			return model.Missing("job not found")
		}
		if j.State == "cancelled" {
			out = j
			return nil
		}
		if j.State != "queued" && j.State != "running" {
			return model.Conflict("job has already finished")
		}
		j.State = "cancelled"
		j.UpdatedAt = time.Now().UTC()
		st.Jobs[id] = j
		st.Emit("job.cancelled", id)
		out = j
		return nil
	})
	return out, e
}
func (s *Service) Retry(id string) (model.Job, error) {
	var out model.Job
	e := s.Store.Update(func(st *model.State) error {
		j, ok := st.Jobs[id]
		if !ok {
			return model.Missing("job not found")
		}
		if j.State != "failed" && j.State != "cancelled" {
			return model.Conflict("only failed or cancelled jobs can retry")
		}
		if e := s.admit(*st, capacity.Footprint(j.Payload, j.Scale)); e != nil {
			return e
		}
		j.State = "queued"
		j.Error = ""
		j.Attempt++
		j.UpdatedAt = time.Now().UTC()
		st.Jobs[id] = j
		st.Emit("job.retried", id)
		out = j
		return nil
	})
	return out, e
}
func (s *Service) Purge(id string) error {
	return s.Store.Update(func(st *model.State) error {
		a, ok := st.Artifacts[id]
		if !ok {
			return model.Missing("artifact not found")
		}
		if a.State == "purged" {
			return nil
		}
		a.State = "purged"
		a.PNG = nil
		st.Artifacts[id] = a
		st.Emit("artifact.purged", id)
		return nil
	})
}
func (s *Service) Artifact(id string) (model.Artifact, error) {
	var a model.Artifact
	ok := false
	s.Store.View(func(st model.State) { a, ok = st.Artifacts[id] })
	if !ok {
		return a, model.Missing("artifact not found")
	}
	if a.State == "purged" {
		return a, model.Gone(fmt.Sprintf("artifact %s was purged", id))
	}
	return a, nil
}
