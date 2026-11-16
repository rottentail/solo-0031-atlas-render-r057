#!/usr/bin/env python3
"""Constrained-environment capacity drill for Atlas Render.

Starts the real server with tight CPU/memory bounds (GOMAXPROCS=1 plus a
soft memory limit) and verifies, against a mixed load rather than one
average scene:

  budget-gate       over-budget renders are rejected with 429 and consume nothing
  asset-gate        decoded-asset memory budget rejects asset-heavy scenes
  scale2            high-multiplier (2x) output renders correctly under limits
  nested            deeply nested layers render deterministically under limits
  stream            a continuous stream of small tasks all complete
  depth-gate        queue depth gate rejects overload; accepted jobs still finish
  bytes-gate        queue byte gate rejects overload by estimated footprint
  control-boundary  health/status stay bounded and cancel works while saturated
  recovery          commitments accepted during overload survive SIGKILL

The drill is self-contained: it builds the binary, uses temporary state
directories and ephemeral ports, and exits non-zero on any violation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

ROOT = Path(__file__).resolve().parents[1]
MIB = 1 << 20
MEMORY_LIMIT = 128 * MIB
QUEUE_DEPTH = 4
QUEUE_BYTES = 36 * MIB
HEAVY_SIZE = 1400
HEAVY_LAYERS = 8
# heavy1: 8 x 1400^2 = 15.7M work pixels, footprint 1400^2*4 = 7.84MB per job.
HEAVY_FOOTPRINT = HEAVY_SIZE * HEAVY_SIZE * 4
# heavy2 (scale 2): 5 x 2800^2 = 39.2M work pixels, footprint 2800^2*4 = 31.36MB.
HEAVY2_FOOTPRINT = (2 * HEAVY_SIZE) * (2 * HEAVY_SIZE) * 4
LATENCY_BOUND_S = 2.0
PALETTE = ['#145A70', '#E87E4F', '#32A5A0', '#FFF4D5', '#7A4E8F', '#C9D64F', '#2F2F4A', '#B33A3A']


def heavy_doc(size=HEAVY_SIZE, layers=HEAVY_LAYERS):
    return {'width': size, 'height': size, 'background': '#F5F1E8', 'layers': [
        {'id': f'sheet-{i}', 'kind': 'rectangle', 'width': size, 'height': size,
         'transform': {'x': 0, 'y': 0, 'scaleX': 1, 'scaleY': 1, 'degrees': i * 3},
         'opacity': .9, 'fill': {'kind': 'linear', 'color': PALETTE[i % len(PALETTE)],
                                 'endColor': PALETTE[(i + 3) % len(PALETTE)],
                                 'from': {'x': 0, 'y': 0}, 'to': {'x': size, 'y': size}}}
        for i in range(layers)]}


def nested_doc():
    node = {'id': 'leaf-8', 'kind': 'ellipse', 'width': 180, 'height': 180,
            'transform': {'x': 20, 'y': 20, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
            'opacity': .8, 'fill': {'kind': 'solid', 'color': PALETTE[7]}}
    for depth in range(8, 0, -1):
        inner = {'id': f'facet-{depth}', 'kind': 'rectangle', 'width': 200, 'height': 200,
                 'transform': {'x': 10, 'y': 10, 'scaleX': 1, 'scaleY': 1, 'degrees': depth * 5},
                 'opacity': .85, 'fill': {'kind': 'solid', 'color': PALETTE[depth % len(PALETTE)]}}
        node = {'id': f'nest-{depth}', 'kind': 'group', 'opacity': .92,
                'transform': {'x': 8, 'y': 8, 'scaleX': .95, 'scaleY': .95, 'degrees': 6},
                'clip': {'x': 0, 'y': 0, 'width': 480, 'height': 480},
                'children': [inner, node]}
    return {'width': 512, 'height': 512, 'background': '#2F2F4A', 'layers': [node]}


def tiny_doc():
    return {'width': 64, 'height': 64, 'background': '#FFFFFF', 'layers': [
        {'id': 'base', 'kind': 'rectangle', 'width': 48, 'height': 48,
         'transform': {'x': 8, 'y': 8, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
         'opacity': 1, 'fill': {'kind': 'solid', 'color': '#145A70'}},
        {'id': 'dot', 'kind': 'ellipse', 'width': 20, 'height': 20,
         'transform': {'x': 22, 'y': 22, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
         'opacity': .9, 'fill': {'kind': 'solid', 'color': '#E87E4F'}}]}


def solid_png(size, rgba):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))
    row = b'\0' + bytes(rgba) * size
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(row * size)) + chunk(b'IEND', b''))


class Server:
    def __init__(self, binary, directory, extra_env):
        self.binary = binary
        self.directory = directory
        self.extra_env = extra_env
        self.proc = None
        directory.mkdir(parents=True, exist_ok=True)
        self.log = open(directory / 'server.log', 'ab')
        self.start()

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        env = dict(os.environ, ATLAS_ADDR=f'127.0.0.1:{port}',
                   ATLAS_STATE=str(self.directory / 'state.json'), **self.extra_env)
        self.proc = subprocess.Popen([str(self.binary)], env=env, stdout=self.log, stderr=self.log)
        for _ in range(200):
            try:
                self.call('GET', '/healthz')
                return
            except (urllib.error.URLError, ConnectionError):
                if self.proc.poll() is not None:
                    raise RuntimeError((self.directory / 'server.log').read_text())
                time.sleep(.03)
        self.stop(abrupt=True)
        raise RuntimeError('server startup deadline exceeded')

    def stop(self, abrupt=False):
        if self.proc and self.proc.poll() is None:
            (self.proc.kill if abrupt else self.proc.terminate)()
            self.proc.wait(timeout=20)

    def call(self, method, path, body=None, status=200, headers=None):
        hdr = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdr['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=hdr)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            if response.status != status:
                raise RuntimeError(f'{method} {path}: expected {status}, received {response.status}: {data[:500]!r}')
            if response.headers.get('Content-Type', '').startswith('application/json'):
                return json.loads(data)
            return data

    def timed(self, method, path):
        start = time.monotonic()
        self.call(method, path)
        return time.monotonic() - start

    def wait(self, job_id, wanted=('succeeded',), deadline_s=120):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            job = self.call('GET', '/v1/jobs/' + job_id)
            if job['state'] in wanted:
                return job
            if job['state'] in ('failed', 'cancelled') and job['state'] not in wanted:
                raise RuntimeError(f'unexpected job outcome: {job}')
            time.sleep(.05)
        raise RuntimeError(f'job {job_id} did not reach {wanted} within {deadline_s}s')

    def put_scene(self, scene_id, doc, rev=0):
        return self.call('PUT', f'/v1/scenes/{scene_id}',
                         {'name': f'演练场景 {scene_id}', 'expectedRevision': rev, 'document': doc})

    def freeze(self, scene_id, rev=1):
        return self.call('POST', f'/v1/scenes/{scene_id}/snapshots', {'expectedRevision': rev}, 201)

    def submit(self, snapshot_id, key, scale=1, status=202):
        return self.call('POST', '/v1/jobs', {'snapshotId': snapshot_id, 'scale': scale}, status,
                         {'Idempotency-Key': key})

    def capacity(self):
        return self.call('GET', '/v1/status')['capacity']


def expect_capacity_rejection(server, snapshot_id, key, scale, phrase):
    body = server.submit(snapshot_id, key, scale, status=429)
    assert body['code'] == 'capacity', f'expected capacity code, got {body}'
    assert phrase in body['error'], f'expected {phrase!r} in {body["error"]!r}'


def phase_budget_gate(server, report):
    """Over-budget work is rejected with 429 and leaves no commitment behind."""
    server.put_scene('gate', heavy_doc(1536, 5))
    snap = server.freeze('gate')
    expect_capacity_rejection(server, snap['id'], 'gate-key', 2, 'work budget')
    assert server.capacity()['queueDepth'] == 0, 'rejected job leaked into the queue'
    job = server.submit(snap['id'], 'gate-key', 1)  # the rejected key must remain usable
    assert server.submit(snap['id'], 'gate-key', 1)['id'] == job['id']
    server.wait(job['id'])
    report['budget-gate'] = {'status': 'pass', 'rejectedScale': 2, 'recoveredJob': job['id']}


def phase_asset_gate(server, report):
    """Decoded-asset memory budget rejects asset-heavy scenes at admission."""
    for i in range(17):
        png = solid_png(1024, (i * 13 % 256, i * 29 % 256, i * 47 % 256, 255))
        server.call('PUT', f'/v1/assets/blob-{i}', png, 201)
    doc = {'width': 64, 'height': 64, 'background': '#FFFFFF', 'layers': [
        {'id': f'img-{i}', 'kind': 'image', 'width': 64, 'height': 64, 'assetId': f'blob-{i}',
         'opacity': 1, 'transform': {'x': 0, 'y': 0, 'scaleX': 1, 'scaleY': 1, 'degrees': 0}}
        for i in range(17)]}
    server.put_scene('asset-heavy', doc)
    snap = server.freeze('asset-heavy')
    expect_capacity_rejection(server, snap['id'], 'asset-key', 1, 'asset memory budget')
    assert server.capacity()['queueDepth'] == 0, 'rejected job leaked into the queue'
    server.put_scene('asset-tiny', tiny_doc())
    tiny_snap = server.freeze('asset-tiny')
    job = server.submit(tiny_snap['id'], 'asset-key')  # the rejected key must remain usable
    server.wait(job['id'])
    report['asset-gate'] = {'status': 'pass', 'decodedBytesRejected': 17 * 1024 * 1024 * 4,
                            'rejectedKeyReused': job['id']}


def phase_scale2(server, report):
    """High-multiplier output renders at full 2x dimensions under constraints."""
    server.put_scene('scale2', heavy_doc(1536, 4))
    snap = server.freeze('scale2')
    job = server.wait(server.submit(snap['id'], 'scale2-key', 2)['id'])
    png = server.call('GET', '/v1/artifacts/' + job['artifactId'])
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert struct.unpack('>II', png[16:24]) == (3072, 3072), 'scale-2 output dimensions wrong'
    report['scale2'] = {'status': 'pass', 'width': 3072, 'height': 3072, 'pngBytes': len(png)}


def phase_nested(server, report):
    """Deeply nested layers render deterministically under constraints."""
    server.put_scene('nested', nested_doc())
    snap = server.freeze('nested')
    first = server.wait(server.submit(snap['id'], 'nested-a', 2)['id'])
    second = server.wait(server.submit(snap['id'], 'nested-b', 2)['id'])
    comparison = server.call('POST', '/v1/compare', {'left': first['artifactId'], 'right': second['artifactId']})
    assert comparison['changedPixels'] == 0, f'nested render not deterministic: {comparison}'
    report['nested'] = {'status': 'pass', 'depth': 8, 'changedPixels': 0}


def phase_stream(server, report):
    """A continuous stream of small tasks all completes under constraints."""
    server.put_scene('tiny', tiny_doc())
    snap = server.freeze('tiny')
    ids = [server.submit(snap['id'], f'stream-{i:02d}')['id'] for i in range(24)]
    for job_id in ids:
        assert server.wait(job_id)['state'] == 'succeeded'
    succeeded = server.call('GET', '/v1/status')['jobs'].get('succeeded', 0)
    assert succeeded >= 24, f'expected at least 24 succeeded jobs, got {succeeded}'
    report['stream'] = {'status': 'pass', 'completed': len(ids)}


def phase_depth_gate(server, report):
    """Queue depth gate rejects overload; accepted commitments still finish."""
    server.put_scene('heavy', heavy_doc())
    snap = server.freeze('heavy')
    jobs = [server.submit(snap['id'], f'depth-{i}')['id'] for i in range(QUEUE_DEPTH)]
    expect_capacity_rejection(server, snap['id'], 'depth-overflow', 1, 'queue is full')
    cap = server.capacity()
    assert cap['queueDepth'] == QUEUE_DEPTH and cap['queueDepthLimit'] == QUEUE_DEPTH
    assert cap['accepting'] is False, 'gate must report not accepting while saturated'
    for job_id in jobs:
        assert server.wait(job_id)['state'] == 'succeeded', 'accepted commitment lost under overload'
    reused = server.submit(snap['id'], 'depth-overflow')
    server.wait(reused['id'])
    assert server.capacity()['accepting'] is True
    report['depth-gate'] = {'status': 'pass', 'accepted': len(jobs), 'rejectedKeyReused': reused['id']}


def phase_bytes_gate(server, report):
    """Queue byte gate rejects overload by estimated resident footprint."""
    server.put_scene('heavy2', heavy_doc(HEAVY_SIZE, 5))
    snap = server.freeze('heavy2')
    job = server.submit(snap['id'], 'bytes-0', 2)
    expect_capacity_rejection(server, snap['id'], 'bytes-1', 2, 'byte budget')
    cap = server.capacity()
    assert cap['queueBytes'] == HEAVY2_FOOTPRINT, f'queue byte accounting off: {cap}'
    assert cap['queueBytesLimit'] == QUEUE_BYTES
    stopped = server.call('POST', '/v1/jobs/' + job['id'] + '/cancel')
    assert stopped['state'] == 'cancelled'
    retried = server.submit(snap['id'], 'bytes-1', 2)
    server.call('POST', '/v1/jobs/' + retried['id'] + '/cancel')
    server.wait(retried['id'], wanted=('cancelled',))
    report['bytes-gate'] = {'status': 'pass', 'footprintBytes': HEAVY2_FOOTPRINT,
                            'rejectedKeyReused': retried['id']}


def phase_control_boundary(server, report):
    """Health and status stay bounded and cancel works while the queue is saturated."""
    server.put_scene('control', heavy_doc())
    snap = server.freeze('control')
    jobs = [server.submit(snap['id'], f'control-{i}')['id'] for i in range(QUEUE_DEPTH)]
    server.wait(jobs[0], wanted=('running',))
    health = max(server.timed('GET', '/healthz') for _ in range(5))
    status = max(server.timed('GET', '/v1/status') for _ in range(5))
    assert health < LATENCY_BOUND_S, f'healthz latency {health:.3f}s exceeds {LATENCY_BOUND_S}s under load'
    assert status < LATENCY_BOUND_S, f'status latency {status:.3f}s exceeds {LATENCY_BOUND_S}s under load'
    cancelled = []
    for job_id in jobs:
        job = server.call('GET', '/v1/jobs/' + job_id)
        if job['state'] in ('queued', 'running'):
            assert server.call('POST', f'/v1/jobs/{job_id}/cancel')['state'] == 'cancelled'
            cancelled.append(job_id)
    for job_id in cancelled:
        server.wait(job_id, wanted=('cancelled',), deadline_s=30)
    assert server.capacity()['queueDepth'] == 0
    report['control-boundary'] = {'status': 'pass', 'healthLatencyMaxMs': round(health * 1000, 1),
                                  'statusLatencyMaxMs': round(status * 1000, 1),
                                  'cancelledUnderLoad': len(cancelled)}


def phase_recovery(server, report):
    """Commitments accepted during overload survive SIGKILL and are recovered."""
    server.put_scene('recover', heavy_doc())
    snap = server.freeze('recover')
    jobs = [server.submit(snap['id'], f'recover-{i}')['id'] for i in range(QUEUE_DEPTH)]
    server.wait(jobs[0], wanted=('running',))
    server.stop(abrupt=True)
    server.start()
    recovered = [e for e in server.call('GET', '/v1/events') if e['kind'] == 'job.recovered']
    assert recovered, 'recovery did not requeue the interrupted running job'
    for job_id in jobs:
        job = server.wait(job_id)
        assert job['attempt'] == 1, f'recovery must not bump attempt: {job}'
    replay = server.submit(snap['id'], 'recover-2')
    assert replay['id'] == jobs[2], 'idempotent receipt did not survive restart'
    assert server.capacity()['queueDepth'] == 0
    report['recovery'] = {'status': 'pass', 'recoveredJobs': len(jobs), 'replayedJob': replay['id']}


def run(binary, temp):
    report = {'drill': 'capacity', 'constraints': {
        'gomaxprocs': 1, 'memoryLimitBytes': MEMORY_LIMIT,
        'queueDepthLimit': QUEUE_DEPTH, 'queueBytesLimit': QUEUE_BYTES}}
    base_env = {'GOMAXPROCS': '1', 'ATLAS_MEMORY_LIMIT': str(MEMORY_LIMIT)}

    print('[drill] config A: default gates, constrained runtime', file=sys.stderr)
    server = Server(binary, Path(temp) / 'config-a', base_env)
    try:
        cap = server.capacity()
        assert cap['gomaxprocs'] == 1, f'GOMAXPROCS not applied: {cap}'
        assert cap['memoryLimitBytes'] == MEMORY_LIMIT, f'memory limit not applied: {cap}'
        assert cap['queueDepthLimit'] == 64 and cap['workPixelLimit'] == 40_000_000
        report['constraints']['reportedByStatus'] = cap
        for phase in (phase_budget_gate, phase_asset_gate, phase_scale2, phase_nested, phase_stream):
            print(f'[drill] phase {phase.__name__}', file=sys.stderr)
            phase(server, report)
    finally:
        server.stop()
        server.log.close()

    print('[drill] config B: tight queue gates, constrained runtime', file=sys.stderr)
    tight_env = dict(base_env, ATLAS_QUEUE_DEPTH=str(QUEUE_DEPTH), ATLAS_QUEUE_BYTES=str(QUEUE_BYTES))
    server = Server(binary, Path(temp) / 'config-b', tight_env)
    try:
        cap = server.capacity()
        assert cap['queueDepthLimit'] == QUEUE_DEPTH and cap['queueBytesLimit'] == QUEUE_BYTES
        for phase in (phase_depth_gate, phase_bytes_gate, phase_control_boundary, phase_recovery):
            print(f'[drill] phase {phase.__name__}', file=sys.stderr)
            phase(server, report)
        jobs = server.call('GET', '/v1/status')['jobs']
        assert 'failed' not in jobs, f'unexpected failed jobs during drill: {jobs}'
        report['finalJobs'] = jobs
    finally:
        server.stop()
        server.log.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='atlas-capacity-') as temp:
        binary = Path(temp) / 'atlas'
        subprocess.run(['go', 'build', '-o', str(binary), './cmd/atlas'], cwd=ROOT, check=True, timeout=120)
        report = run(binary, temp)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
