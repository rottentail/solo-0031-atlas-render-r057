package httpapi

import (
	"atlas-render/internal/bundle"
	"atlas-render/internal/model"
	"io"
	"net/http"
)

func (a API) putScene(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name     string         `json:"name"`
		Expected int            `json:"expectedRevision"`
		Document model.Document `json:"document"`
	}
	if e := decode(w, r, &body); e != nil {
		fail(w, e)
		return
	}
	v, e := a.Service.PutScene(r.PathValue("id"), body.Name, body.Expected, body.Document)
	if e != nil {
		fail(w, e)
		return
	}
	write(w, 200, v)
}
func (a API) getScene(w http.ResponseWriter, r *http.Request) {
	v, ok := model.Scene{}, false
	a.Service.Store.View(func(st model.State) { v, ok = st.Scenes[r.PathValue("id")] })
	if !ok {
		fail(w, model.Missing("scene not found"))
		return
	}
	write(w, 200, v)
}
func (a API) sceneState(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Expected int    `json:"expectedRevision"`
		State    string `json:"state"`
	}
	if e := decode(w, r, &b); e != nil {
		fail(w, e)
		return
	}
	v, e := a.Service.SceneState(r.PathValue("id"), b.Expected, b.State)
	if e != nil {
		fail(w, e)
		return
	}
	write(w, 200, v)
}
func (a API) freeze(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Expected int `json:"expectedRevision"`
	}
	if e := decode(w, r, &b); e != nil {
		fail(w, e)
		return
	}
	v, e := a.Service.Freeze(r.PathValue("id"), b.Expected)
	if e != nil {
		fail(w, e)
		return
	}
	write(w, 201, v)
}
func (a API) getSnapshot(w http.ResponseWriter, r *http.Request) {
	v, ok := model.Snapshot{}, false
	a.Service.Store.View(func(st model.State) { v, ok = st.Snapshots[r.PathValue("id")] })
	if !ok {
		fail(w, model.Missing("snapshot not found"))
		return
	}
	write(w, 200, v)
}
func (a API) revoke(w http.ResponseWriter, r *http.Request) {
	if e := a.Service.Revoke(r.PathValue("id")); e != nil {
		fail(w, e)
		return
	}
	write(w, 200, map[string]string{"state": "revoked"})
}
func (a API) importAsset(w http.ResponseWriter, r *http.Request) {
	b, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 2<<20))
	if e != nil {
		fail(w, model.Invalid("asset body too large"))
		return
	}
	v, e := a.Service.ImportAsset(r.PathValue("id"), b)
	if e != nil {
		fail(w, e)
		return
	}
	write(w, 201, v)
}
func (a API) retireAsset(w http.ResponseWriter, r *http.Request) {
	if e := a.Service.RetireAsset(r.PathValue("id")); e != nil {
		fail(w, e)
		return
	}
	write(w, 200, map[string]string{"state": "retired"})
}
func (a API) exportPackage(w http.ResponseWriter, r *http.Request) {
	v, e := a.Service.Export(r.PathValue("id"))
	if e != nil {
		fail(w, e)
		return
	}
	write(w, 200, v)
}
func (a API) importPackage(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Name    string         `json:"name"`
		Package bundle.Package `json:"package"`
	}
	if e := decode(w, r, &b); e != nil {
		fail(w, e)
		return
	}
	v, e := a.Service.Import(r.PathValue("id"), b.Name, b.Package)
	if e != nil {
		fail(w, e)
		return
	}
	write(w, 201, v)
}
