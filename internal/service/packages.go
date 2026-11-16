package service

import (
	"atlas-render/internal/bundle"
	"atlas-render/internal/model"
	"atlas-render/internal/scene"
	"time"
)

func (s *Service) Export(id string) (bundle.Package, error) {
	var v model.Snapshot
	ok := false
	s.Store.View(func(st model.State) { v, ok = st.Snapshots[id] })
	if !ok {
		return bundle.Package{}, model.Missing("snapshot not found")
	}
	return bundle.Export(v), nil
}
func (s *Service) Import(id, name string, p bundle.Package) (model.Scene, error) {
	var out model.Scene
	if !scene.ValidID(id) || len(name) < 1 || len(name) > 160 {
		return out, model.Invalid("invalid import destination")
	}
	if e := bundle.Validate(p); e != nil {
		return out, e
	}
	e := s.Store.Update(func(st *model.State) error {
		if _, ok := st.Scenes[id]; ok {
			return model.Conflict("destination scene already exists")
		}
		for aid, a := range p.Snapshot.Assets {
			if existing, ok := st.Assets[aid]; ok {
				if existing.Digest != a.Digest || existing.State != "available" {
					return model.Conflict("destination asset conflicts with package")
				}
			} else {
				a.State = "available"
				st.Assets[aid] = a
			}
		}
		out = model.Scene{ID: id, Name: name, Revision: 1, State: "active", Document: p.Snapshot.Document, UpdatedAt: time.Now().UTC()}
		st.Scenes[id] = out
		st.Emit("scene.imported", id)
		return nil
	})
	return out, e
}
