// Package capacity 汇总上线容量门禁阈值与运行时资源边界。
// 门禁分两层：单任务预算（工作像素、解码素材字节）在编译期核对，
// 队列预算（未结束任务数量、驻留字节估计）在接受承诺前核对。
// 所有阈值可用环境变量收紧，默认值与历史行为一致。
package capacity

import (
	"atlas-render/internal/model"
	"fmt"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
)

// Limits 是接收新承诺前必须全部通过的容量门禁。
type Limits struct {
	WorkPixels  int64 // 单任务编译工作量（图层像素）上限
	QueueDepth  int   // 未结束任务（queued+running）数量上限
	QueueBytes  int64 // 未结束任务驻留字节估计上限
	AssetBytes  int64 // 单任务解码素材字节上限
	MemoryLimit int64 // 进程软内存上限字节；0 表示不主动设置
}

// Default 返回与既有行为一致的基线门禁。
func Default() Limits {
	return Limits{WorkPixels: 40_000_000, QueueDepth: 64, QueueBytes: 256 << 20, AssetBytes: 64 << 20}
}

// FromEnv 用 ATLAS_* 环境变量覆盖默认门禁；非法取值使启动失败。
//
//	ATLAS_WORK_PIXELS   单任务工作像素上限
//	ATLAS_QUEUE_DEPTH   未结束任务数量上限
//	ATLAS_QUEUE_BYTES   未结束任务驻留字节估计上限
//	ATLAS_ASSET_BYTES   单任务解码素材字节上限
//	ATLAS_MEMORY_LIMIT  进程软内存上限字节（GOMEMLIMIT 已设置时不生效）
func FromEnv() (Limits, error) {
	lm := Default()
	fields := []struct {
		name string
		dst  *int64
	}{
		{"ATLAS_WORK_PIXELS", &lm.WorkPixels},
		{"ATLAS_QUEUE_BYTES", &lm.QueueBytes},
		{"ATLAS_ASSET_BYTES", &lm.AssetBytes},
		{"ATLAS_MEMORY_LIMIT", &lm.MemoryLimit},
	}
	for _, f := range fields {
		if e := applyEnv(f.name, f.dst); e != nil {
			return lm, e
		}
	}
	if raw := os.Getenv("ATLAS_QUEUE_DEPTH"); raw != "" {
		v, e := strconv.ParseInt(raw, 10, 32)
		if e != nil || v <= 0 {
			return lm, fmt.Errorf("ATLAS_QUEUE_DEPTH must be a positive integer, got %q", raw)
		}
		lm.QueueDepth = int(v)
	}
	return lm, nil
}

func applyEnv(name string, dst *int64) error {
	raw := os.Getenv(name)
	if raw == "" {
		return nil
	}
	v, e := strconv.ParseInt(raw, 10, 64)
	if e != nil || v <= 0 {
		return fmt.Errorf("%s must be a positive integer of bytes/pixels, got %q", name, raw)
	}
	*dst = v
	return nil
}

// String 以单行摘要报告门禁，供启动日志记录实际生效值。
func (lm Limits) String() string {
	return fmt.Sprintf("workPixels=%d queueDepth=%d queueBytes=%d assetBytes=%d memoryLimit=%d",
		lm.WorkPixels, lm.QueueDepth, lm.QueueBytes, lm.AssetBytes, lm.MemoryLimit)
}

// ApplyMemory 应用进程软内存上限。标准 GOMEMLIMIT 环境变量优先：
// 运行时已按它初始化，这里不再覆盖；否则按 ATLAS_MEMORY_LIMIT 设置。
// 返回实际生效的内存上限字节。
func (lm Limits) ApplyMemory() int64 {
	if os.Getenv("GOMEMLIMIT") == "" && lm.MemoryLimit > 0 {
		debug.SetMemoryLimit(lm.MemoryLimit)
	}
	return debug.SetMemoryLimit(-1)
}

// Runtime 报告进程实际生效的运行时资源边界。
type Runtime struct {
	GOMAXPROCS  int   `json:"gomaxprocs"`
	MemoryLimit int64 `json:"memoryLimitBytes"`
}

// CurrentRuntime 读取运行时边界；memoryLimitBytes 为 math.MaxInt64 表示未设上限。
func CurrentRuntime() Runtime {
	return Runtime{GOMAXPROCS: runtime.GOMAXPROCS(0), MemoryLimit: debug.SetMemoryLimit(-1)}
}

// Footprint 估计一个已接受承诺的驻留字节：固定输入中的素材字节
// 加上输出像素缓冲（宽×高×4）。用于队列字节门禁，是保守上界而非精确值。
func Footprint(snap model.Snapshot, scale int) int64 {
	b := int64(snap.Document.Width*scale) * int64(snap.Document.Height*scale) * 4
	for _, a := range snap.Assets {
		b += int64(len(a.PNG))
	}
	return b
}

// QueueUsage 统计未结束承诺的数量与驻留字节估计。
func QueueUsage(st model.State) (depth int, bytes int64) {
	for _, j := range st.Jobs {
		if j.State == "queued" || j.State == "running" {
			depth++
			bytes += Footprint(j.Payload, j.Scale)
		}
	}
	return depth, bytes
}
