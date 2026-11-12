#!/usr/bin/env python3
"""Fake Topaz video API for demo/testing (python3 app.py --demo). No credits, nothing leaves your machine.

Follows the documented flow: POST /video/ -> PATCH /{id}/accept -> PUT upload url(s) -> PATCH /{id}/complete-upload/
-> GET /{id}/status (progress 0..100) -> download url. It checks request bodies the way the docs describe and
"processes" by handing your own file back, so the whole download + rename path can be tried end to end.
Env: MOCK_SECONDS (fake processing time, default 8), MOCK_PART_BYTES (multi-part size, default 500 MB),
MOCK_DL_SLEEP (seconds to stretch each download, default 0).
"""
import json
import math
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PART = int(os.environ.get('MOCK_PART_BYTES', 500 * 1024 * 1024))
SECONDS = float(os.environ.get('MOCK_SECONDS', 8))
DL_SLEEP = float(os.environ.get('MOCK_DL_SLEEP', 0))
INTERP = {'apo-8', 'apf-2', 'chr-2', 'chf-3', 'aion-1'}
CONTAINERS = {'3gp', 'avi', 'dv', 'flv', 'm1v', 'm2t', 'm2ts', 'm2v', 'm4v', 'mkv', 'mov', 'mp4', 'mpeg', 'mpg', 'mts',
              'mxf', 'ser', 'ts', 'vob', 'webm', 'wmv'}
REQ = {}
CREDITS = {}          # per API key: accepts left; missing = MOCK_CREDITS (-1 = unlimited)
DEFAULT_CREDITS = int(os.environ.get('MOCK_CREDITS', -1))
TRUNCATE = [0]        # next N downloads come back cut in half (to test completeness checks)
FAIL = [0]            # next N uploaded jobs end as status failed (topaz refunds those)
ACCEPT500 = [0]       # next N accepts go through but answer 500 (lost answer: the retry then sees 409)
LOG = []
LOCK = threading.Lock()


def validate(b):
    src, flt, out = b.get('source') or {}, b.get('filters'), b.get('output') or {}
    for k in ('container', 'size', 'duration', 'frameCount', 'frameRate', 'resolution'):
        if k not in src:
            return f'source.{k} required'
    if str(src['container']).lower() not in CONTAINERS:
        return 'bad source.container'
    if not isinstance(flt, list) or not flt or not all(isinstance(f, dict) and f.get('model') for f in flt):
        return 'filters must be a non-empty list of {model: ...}'
    for f in flt:
        if f['model'] == 'slp-2.6' and 'sharpness' in f and not 1.0 <= float(f['sharpness']) <= 5.0:
            return 'sharpness out of range'
        if f['model'] in INTERP:
            if 'fps' in f and not 15 <= float(f['fps']) <= 240:
                return 'fps out of range'
            if 'slowmo' in f and not 1 <= float(f['slowmo']) <= 16:
                return 'slowmo out of range'
    for k in ('resolution', 'frameRate', 'audioTransfer'):
        if k not in out:
            return f'output.{k} required'
    if out['audioTransfer'] != 'None' and 'audioCodec' not in out:
        return 'output.audioCodec required when audioTransfer is not None'
    has_interp = any(f['model'] in INTERP for f in flt)
    if not has_interp and abs(float(out['frameRate']) - float(src['frameRate'])) > 0.01:
        return 'note: without an interpolation model output.frameRate is forced to source fps'
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj, headers=None):
        d = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(d)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(d)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def _key(self):
        if not self.headers.get('X-API-Key'):
            self._j(401, {'error': 'missing X-API-Key'})
            return False
        return True

    def do_POST(self):
        if self.path in ('/_mock/credits', '/_mock/truncate', '/_mock/fail', '/_mock/accept500'):   # test controls
            b = json.loads(self._body() or b'{}')
            with LOCK:
                if self.path == '/_mock/credits':
                    CREDITS[b['key']] = int(b['n'])
                else:
                    {'/_mock/truncate': TRUNCATE, '/_mock/fail': FAIL, '/_mock/accept500': ACCEPT500}[self.path][0] = int(b['n'])
            return self._j(200, {'ok': True})
        if self.path.rstrip('/') != '/video' or not self._key():
            return self._j(404, {'error': 'not found'})
        raw = self._body()
        if len(raw) > 3000:
            self.send_response(413)
            self.end_headers()
            return
        b = json.loads(raw)
        err = validate(b)
        with LOCK:
            LOG.append({'op': 'create', 'body': b, 'error': err})
        if err and not err.startswith('note:'):
            return self._j(400, {'error': err})
        rid = str(uuid.uuid4())
        out_frames = int(b['source']['frameCount'])
        if any(f['model'] in INTERP for f in b['filters']):
            out_frames = int(out_frames * float(b['output']['frameRate']) / float(b['source']['frameRate']))
        cost = max(1, math.ceil(out_frames / 12))
        with LOCK:
            REQ[rid] = {'body': b, 'state': 'requested', 'parts': {}, 'polls': 0, 'key': self.headers.get('X-API-Key')}
        self._j(200, {'requestId': rid, 'estimates': {'cost': [cost, int(cost * 1.1) + 1], 'time': [20, 30]}})

    def do_PATCH(self):
        if not self._key():
            return
        parts = [p for p in self.path.split('/') if p]
        raw = self._body()
        if len(parts) < 3 or parts[0] != 'video' or parts[1] not in REQ or \
                REQ[parts[1]]['key'] != self.headers.get('X-API-Key'):
            return self._j(404, {'error': 'unknown request'})
        r = REQ[parts[1]]
        if parts[2] == 'accept':
            if r['state'] != 'requested':
                return self._j(409, {'error': 'already accepted'})
            k = self.headers.get('X-API-Key')
            with LOCK:
                left = CREDITS.get(k, DEFAULT_CREDITS)
                if left == 0:
                    LOG.append({'op': 'accept-402', 'id': parts[1]})
                    return self._j(402, {'error': 'Insufficient credits'})
                if left > 0:
                    CREDITS[k] = left - 1
            n = max(1, math.ceil(int(r['body']['source']['size']) / PART))
            host = self.headers.get('Host')
            r['state'] = 'accepted'
            with LOCK:
                LOG.append({'op': 'accept', 'id': parts[1], 'parts': n})
                if ACCEPT500[0] > 0:
                    ACCEPT500[0] -= 1
                    return self._j(500, {'error': 'internal error'})
            return self._j(200, {'urls': [f'http://{host}/upload/{parts[1]}/{i + 1}?sig=fake' for i in range(n)]})
        if parts[2] == 'complete-upload':
            res = json.loads(raw or b'{}').get('uploadResults') or []
            want = {str(k): v[1] for k, v in r['parts'].items()}
            got = {str(x.get('partNum')): x.get('eTag') for x in res}
            if not want or got != want:
                return self._j(400, {'error': f'uploadResults mismatch: {got} vs {want}'})
            r['state'], r['t0'] = 'processing', time.time()
            with LOCK:
                if FAIL[0] > 0:
                    FAIL[0] -= 1
                    r['fail'] = True
            with LOCK:
                LOG.append({'op': 'complete-upload', 'id': parts[1], 'parts': len(res)})
            return self._j(200, {'message': 'ok'})
        self._j(404, {'error': 'not found'})

    def do_PUT(self):
        parts = [p for p in self.path.split('?')[0].split('/') if p]
        if len(parts) != 3 or parts[0] != 'upload' or parts[1] not in REQ:
            return self._j(404, {'error': 'bad upload url'})
        data = self._body()
        etag = '"' + uuid.uuid4().hex + '"'
        REQ[parts[1]]['parts'][int(parts[2])] = (data, etag)
        self.send_response(200)
        self.send_header('ETag', etag)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        parts = [p for p in self.path.split('?')[0].split('/') if p]
        if parts == ['_mock', 'log']:
            with LOCK:
                return self._j(200, LOG)
        if len(parts) == 2 and parts[0] == 'download' and parts[1] in REQ:
            r = REQ[parts[1]]
            data = b''.join(r['parts'][k][0] for k in sorted(r['parts']))
            with LOCK:
                if TRUNCATE[0] > 0:
                    TRUNCATE[0] -= 1
                    data = data[:len(data) // 2]
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            step = max(1, len(data) // 4)
            for i in range(0, len(data), step):
                self.wfile.write(data[i:i + step])
                self.wfile.flush()
                time.sleep(DL_SLEEP / 4)
            return
        if len(parts) == 3 and parts[0] == 'video' and parts[2] == 'status':
            if not self._key():
                return
            r = REQ.get(parts[1])
            if not r or r['key'] != self.headers.get('X-API-Key'):
                return self._j(404, {'error': 'unknown request'})
            r['polls'] += 1
            if r['polls'] == 1:  # first poll is rate limited once, to exercise backoff
                return self._j(429, {'error': 'slow down'}, {'Retry-After': '1'})
            if r.get('fail'):
                return self._j(200, {'status': 'failed', 'progress': 0, 'message': 'Processing failed', 'errorCode': 'SERVER_ERROR'})
            if r['state'] != 'processing':
                return self._j(200, {'status': r['state'], 'progress': 0})
            p = min(100.0, 100.0 * (time.time() - r['t0']) / SECONDS)
            if p >= 100:
                host = self.headers.get('Host')
                return self._j(200, {'status': 'complete', 'progress': 100, 'message': 'Processing complete!',
                                     'download': {'url': f'http://{host}/download/{parts[1]}'}})
            return self._j(200, {'status': 'processing', 'progress': round(p, 2)})
        self._j(404, {'error': 'not found'})


def start_background(port=0):
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{srv.server_address[1]}/video'


if __name__ == '__main__':
    print('mock topaz at', start_background(int(os.environ.get('MOCK_PORT', 8766))))
    while True:
        time.sleep(3600)
