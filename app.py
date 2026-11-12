#!/usr/bin/env python3
"""Topaz Batch Upscaler - a local app for running a folder of clips through the Topaz video API.

    python3 app.py            -> open http://127.0.0.1:8765
    python3 app.py --demo     -> same UI against a fake local Topaz API (no key, no credits)

Python 3.8+ standard library only. Needs ffprobe (comes with ffmpeg) on PATH.
Your API key stays on this machine: it is only ever sent to api.topazlabs.com.
"""
import argparse
import hashlib
import http.client
import json
import os
import platform
import random
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOPAZ_BASE = 'https://api.topazlabs.com/video'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
CONF_DIR = os.path.join(os.path.expanduser('~'), '.topaz_batch')
KEY_FILE = os.path.join(CONF_DIR, 'api_key')
SETTINGS_FILE = os.path.join(CONF_DIR, 'settings.json')
STATE_NAME = '.topaz_batch_state.json'   # lives in the OUTPUT folder, never holds the key

# containers the API accepts as source (docs: request-parameters/video)
VIDEO_EXT = {'3gp', 'avi', 'dv', 'flv', 'm1v', 'm2t', 'm2ts', 'm2v', 'm4v', 'mkv', 'mov', 'mp4', 'mpeg',
             'mpg', 'mts', 'mxf', 'ser', 'ts', 'vob', 'webm', 'wmv'}
UPSCALE_MODELS = {'slp-2.6': 'Starlight Precise 2.6'}
INTERP_MODELS = {'apo-8': 'Apollo', 'apf-2': 'Apollo Fast', 'chr-2': 'Chronos', 'chf-3': 'Chronos Fast',
                 'aion-1': 'Aion'}
# one-line hints, from developer.topazlabs.com/video-models/frame-interpolation*.md
INTERP_HINTS = {
    'auto': 'Apollo, only on clips whose fps actually changes; clips already at the target fps skip it (no extra cost)',
    'apo-8': 'most precise fps conversion + slow motion, best at exact multiples (24 to 48, 30 to 60)',
    'apf-2': 'quicker Apollo at about half the cost, for simple motion',
    'chr-2': 'fps conversion between odd rates without jitter (24 to 30, 25 to 30) + slow motion',
    'chf-3': 'quicker Chronos, good for long clips',
    'aion-1': 'extreme slow motion on complex, fast, high-res motion; most expensive',
}
AUTO_ENGINE = 'apo-8'
FPS_PRESETS = [23.976, 24, 25, 29.97, 30, 48, 50, 60]
ENCODERS = {'H264': ['High'], 'H265': ['Main10', 'Main'], 'ProRes': ['422 HQ', '422', '422 LT', '422 Proxy'],
            'AV1': ['8-bit', '10-bit']}
ENCODER_MAX = {'H264': 4096, 'H265': 8192, 'ProRes': 16386, 'AV1': 16384}
RES_BOXES = {'4k': (3840, 2160), '1440p': (2560, 1440), '1080p': (1920, 1080)}
DEFAULTS = {
    'model': 'slp-2.6', 'custom_model': '', 'sharpness': 3.0, 'extra_params': '',
    'res_mode': '4k', 'custom_w': 3840, 'custom_h': 2160,
    'out_fps': 0.0, 'interp': 'auto', 'slowmo': 1.0, 'duplicate': False,   # out_fps 0 = keep source fps
    'encoder': 'H264', 'profile': 'High', 'compression': 'High', 'container': 'mp4', 'audio': 'Copy',
    'parallel': 3, 'poll_seconds': 10, 'skip_existing': True,
    'input_dir': '', 'output_dir': '',
}
ACTIVE = {'creating', 'uploading', 'upscaling', 'downloading'}
SETTINGS_V = 4
CREDIT_WORDS = ('credit', 'insufficient', 'balance', 'quota', 'payment required', 'out of funds')


def key_fp(key):
    """Short one-way fingerprint so the state file can tell which key submitted a job. Never the key itself."""
    return hashlib.sha256(key.encode()).hexdigest()[:10] if key else None


class CreditError(Exception):
    pass


class TopazFailed(Exception):
    pass


class OldKeyLost(Exception):
    pass


class AcceptUnknown(Exception):
    """PATCH /accept did not give a clean answer (timeout, 5xx then 409): Topaz may have reserved credits."""


class DuplicateSubmit(Exception):
    """A second paid submit for the same file was about to happen. Never allowed without the user's ok."""


class NeedConfirm(Exception):
    def __init__(self, info):
        super().__init__('confirm the credit total first')
        self.info = info


# v4 money rule: every request id Topaz ever reserved credits for is kept in job['paid'] and never dropped.
# A new accept for a file only happens when it has no live paid job, or the user hit "re-run (charges again)".
LIVE_PAID = ('accepted', 'accept-unknown', 'other-account', 'complete')
HOLD_MSG = {
    'other-account': 'paid on the account of the key you used before. put that key back and hit "fetch finished" '
                     'to download it, no new charge. re-run here charges THIS account again',
    'failed': 'topaz failed this job (topaz refunds failed jobs). re-run charges again',
    'no-upload': 'topaz took the accept but the upload never finished. topaz times it out and refunds it by '
                 'itself. after that, re-run (charges again)',
    'done-missing': 'finished and paid in an earlier run, but the output is not in this folder. hit "fetch '
                    'finished" to download it again free (topaz keeps it 7 days)',
    'dup': 'this file already has a paid job at topaz - fetch finished or resume it. re-run charges again',
}


def live_paid(job):
    return [p for p in (job.get('paid') or []) if p.get('status') in LIVE_PAID]


def mark_paid(job, rid, status):
    for p in job.get('paid') or []:
        if p.get('id') == rid:
            p['status'] = status


def is_credit_error(e):
    t = str(e).lower()
    if isinstance(e, ApiError):
        return e.code == 402 or (e.code in (400, 403, 409, 422) and any(w in t for w in CREDIT_WORDS))
    return isinstance(e, (TopazFailed, CreditError)) and any(w in t for w in CREDIT_WORDS)


def log(msg):
    APP.add_log(msg)


# ----------------------------------------------------------------------------- media probing
def parse_rate(r):
    try:
        if '/' in str(r):
            a, b = str(r).split('/')
            return float(a) / float(b) if float(b) else 0.0
        return float(r)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path):
    """Read width/height/fps/frames/duration with ffprobe. Raises RuntimeError on failure."""
    if not shutil.which('ffprobe'):
        raise RuntimeError('ffprobe not found - install ffmpeg (mac: brew install ffmpeg)')
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
           'stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:stream_side_data=rotation'
           ':stream_tags=rotate:format=duration', '-of', 'json', path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError('ffprobe failed: ' + (p.stderr.strip()[:200] or 'unknown error'))
    d = json.loads(p.stdout or '{}')
    if not d.get('streams'):
        raise RuntimeError('no video stream')
    s = d['streams'][0]
    w, h = int(s.get('width') or 0), int(s.get('height') or 0)
    rot = 0
    for sd in s.get('side_data_list') or []:
        if 'rotation' in sd:
            rot = int(float(sd['rotation']))
    if not rot and (s.get('tags') or {}).get('rotate'):
        rot = int(float(s['tags']['rotate']))
    if abs(rot) % 180 == 90:
        w, h = h, w
    fps = parse_rate(s.get('avg_frame_rate')) or parse_rate(s.get('r_frame_rate'))
    dur = float(s.get('duration') or (d.get('format') or {}).get('duration') or 0)
    frames = int(s['nb_frames']) if str(s.get('nb_frames', '')).isdigit() else 0
    if not frames:
        c = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries',
                            'stream=nb_read_packets', '-of', 'csv=p=0', path], capture_output=True, text=True,
                           timeout=600)
        frames = int(c.stdout.strip().split(',')[0]) if c.stdout.strip().split(',')[0].isdigit() else 0
    if not frames and dur and fps:
        frames = int(round(dur * fps))
    if not dur and frames and fps:
        dur = frames / fps
    if not (w and h and fps and frames and dur):
        raise RuntimeError(f'could not read video metadata (w={w} h={h} fps={fps:.3f} frames={frames})')
    return {'w': w, 'h': h, 'fps': round(fps, 3), 'frames': frames, 'duration': round(dur, 3)}


def probe_quick(path):
    """(width, height, duration) of an output file, or raises RuntimeError. One fast ffprobe call."""
    p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=width,height,duration:format=duration,start_time', '-of', 'json', path],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError('unreadable (' + (p.stderr.strip().splitlines() or ['no detail'])[-1][:120] + ')')
    d = json.loads(p.stdout or '{}')
    if not d.get('streams'):
        raise RuntimeError('no video stream')
    s = d['streams'][0]
    try:   # the VIDEO stream's length (format duration can be the longer audio track)
        dur = float(s.get('duration') or (d.get('format') or {}).get('duration') or 0)
    except ValueError:
        dur = 0.0
    try:   # pts in the file are offset by the start time; fold it in so the tail check compares like with like
        dur += float((d.get('format') or {}).get('start_time') or 0)
    except ValueError:
        pass
    return int(s.get('width') or 0), int(s.get('height') or 0), dur


def check_output(path, meta, slowmos=(1.0,)):
    """Is this output file a COMPLETE upscale of the clip? -> (True, '') or (False, why).
    Complete = more than 0 bytes, ffprobe reads it, duration within max(0.5s, 2%) of the source (x slow motion)."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f'cannot read ({e})'
    if size <= 0:
        return False, '0 bytes'
    try:
        w, h, dur = probe_quick(path)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if not (w and h):
        return False, 'no picture size'
    if not dur:
        return False, 'no duration (cut off)'
    src = float(meta.get('duration') or 0) if meta else 0.0
    if src:
        diffs = [abs(dur - src * k) for k in set(float(k) for k in slowmos)]
        tol = max(0.5, 0.02 * src * max(slowmos))
        if min(diffs) > tol:
            return False, f'{dur:.1f}s long, source is {src:.1f}s'
    # the header can claim the full length on a cut-off file (index written first), so read the real last frames
    last = tail_pts(path, dur)
    if last is None or last < dur - max(0.5, 0.02 * dur):
        return False, 'cut off: the last frames are missing'
    return True, ''


def tail_pts(path, dur):
    """Time of the last video packet actually present, read from the last keyframe to the end of the file."""
    try:
        p = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-read_intervals',
                            f'{max(0.0, dur - 0.1):.3f}%', '-show_entries', 'packet=pts_time', '-of', 'csv=p=0', path],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    vals = []
    for line in p.stdout.split():
        try:
            vals.append(float(line.strip(',')))
        except ValueError:
            pass
    return max(vals) if vals else None


def output_index(out_dir):
    """casefolded file stem -> [paths], for video files in the output folder and its direct subfolders."""
    idx = {}
    dirs = [out_dir]
    try:
        dirs += [os.path.join(out_dir, d) for d in sorted(os.listdir(out_dir))
                 if not d.startswith('.') and os.path.isdir(os.path.join(out_dir, d))]
    except OSError:
        pass
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            stem, ext = os.path.splitext(n)
            p = os.path.join(d, n)
            if n.startswith('.') or ext.lower().lstrip('.') not in VIDEO_EXT or not os.path.isfile(p):
                continue
            idx.setdefault(stem.casefold(), []).append(p)
    return idx


def match_output(idx, src_name, meta, slowmos):
    """Find an existing output for this clip by name stem, whatever its extension.
    Returns ('done', path, '') | ('partial', path, why) | (None, None, '')."""
    stem = os.path.splitext(src_name)[0]
    partial = None
    # <stem>_upscaled.<any video ext> first; then a file renamed back to the clip's own name, which only
    # counts if it is bigger than the source (so a plain copy of the original never passes as done)
    for key, need_bigger in ((f'{stem}_upscaled'.casefold(), False), (stem.casefold(), True)):
        for p in idx.get(key, []):
            good, why = check_output(p, meta, slowmos)
            if good and need_bigger and meta:
                try:
                    w, h, _ = probe_quick(p)
                except Exception:  # noqa: BLE001
                    w = h = 0
                if w * h <= meta['w'] * meta['h']:
                    continue   # same size as the source: a copy, not an upscale
            if good:
                return 'done', p, ''
            if not need_bigger and partial is None:
                partial = (p, why)
    if partial:
        return 'partial', partial[0], partial[1]
    return None, None, ''


# ----------------------------------------------------------------------------- request building
def target_resolution(w, h, s):
    mode = s['res_mode']
    if mode == 'source':
        W, H = w, h
    elif mode in ('2x', '4x'):
        f = int(mode[0])
        W, H = w * f, h * f
    elif mode == 'custom':
        W, H = int(s['custom_w']), int(s['custom_h'])
    else:
        bw, bh = RES_BOXES[mode]
        if h > w:  # portrait clip -> portrait box
            bw, bh = bh, bw
        sc = min(bw / w, bh / h)
        W, H = w * sc, h * sc
    # the API rounds up to a multiple of 4; do it here so the board shows the real size
    return int(-(-round(W) // 4) * 4), int(-(-round(H) // 4) * 4)


def out_container(s):
    if s['encoder'] == 'ProRes':
        return 'mov'
    if s['encoder'] == 'AV1':
        return 'mp4'
    return s['container']


def output_name(src_name, s):
    stem = os.path.splitext(src_name)[0]
    return f'{stem}_upscaled.{out_container(s)}'


def fps_plan(m, s):
    """Per clip: source fps -> output fps and which interpolation engine (None = no interpolation filter).
    Raises ValueError for values the API would reject."""
    src = float(m['fps'])
    want = float(s.get('out_fps') or 0)
    if want and not 15 <= want <= 240:
        raise ValueError('output fps must be 15 to 240 (or keep source)')
    slowmo = float(s.get('slowmo') or 1)
    if not 1 <= slowmo <= 16:
        raise ValueError('slow motion must be 1 to 16')
    out = want or src
    changes = abs(out - src) > 0.005
    eng = s.get('interp') or 'auto'
    if eng in ('none', 'auto'):
        eng = AUTO_ENGINE if (changes or slowmo > 1 or s.get('duplicate')) else None
    elif eng not in INTERP_MODELS:
        raise ValueError(f'unknown fps engine {eng}')
    elif not (changes or slowmo > 1 or s.get('duplicate')):
        eng = None   # nothing to interpolate: skip the filter, saves credits
    if eng and not 15 <= out <= 240:
        raise ValueError(f'source is {src:g} fps; interpolation output must be 15 to 240 - pick an output fps')
    return {'src': round(src, 3), 'out': round(out, 3), 'engine': eng, 'slowmo': slowmo}


def build_body(job, s):
    """The exact JSON sent to POST /video/. Raises ValueError for settings the API would reject."""
    m = job['meta']
    model = s['custom_model'].strip() if s['model'] == 'custom' else s['model']
    if not model:
        raise ValueError('upscale model id is empty')
    f1 = {'model': model}
    sharp = float(s['sharpness'])
    if not 1.0 <= sharp <= 5.0:
        raise ValueError('sharpness must be 1.0 to 5.0')
    if model == 'slp-2.6':
        f1['sharpness'] = sharp
    if s.get('extra_params', '').strip():
        try:
            extra = json.loads(s['extra_params'])
        except ValueError:
            raise ValueError('extra model params is not valid JSON')
        if not isinstance(extra, dict):
            raise ValueError('extra model params must be a JSON object')
        f1.update(extra)
        f1['model'] = model
    filters = [f1]
    plan = fps_plan(m, s)
    if plan['engine']:
        f2 = {'model': plan['engine']}
        if plan['slowmo'] > 1:
            f2['slowmo'] = plan['slowmo']
        if s.get('duplicate'):
            f2['duplicate'] = True
        filters.append(f2)   # upscale first, then interpolate
    out_fps = plan['out']    # Topaz honours a new frameRate only when an interpolation model is in filters
    W, H = target_resolution(m['w'], m['h'], s)
    enc = s['encoder']
    if max(W, H) > ENCODER_MAX[enc]:
        raise ValueError(f'{W}x{H} is above the {enc} limit ({ENCODER_MAX[enc]}px) - pick H265 or ProRes')
    out = {'resolution': {'width': W, 'height': H}, 'frameRate': out_fps, 'audioTransfer': s['audio'],
           'videoEncoder': enc, 'videoProfile': s['profile'] if s['profile'] in ENCODERS[enc] else ENCODERS[enc][0],
           'container': out_container(s)}
    if s['audio'] != 'None':
        out['audioCodec'] = 'AAC'
    if enc in ('H264', 'H265', 'AV1'):
        out['dynamicCompressionLevel'] = s['compression']
    body = {'source': {'container': job['container'], 'size': job['size'], 'duration': m['duration'],
                       'frameRate': m['fps'], 'frameCount': m['frames'],
                       'resolution': {'width': m['w'], 'height': m['h']}},
            'filters': filters, 'output': out}
    if len(json.dumps(body)) > 3000:
        raise ValueError('request body over 3000 bytes - trim extra model params')
    return body


def settings_hash(s):
    keys = ['model', 'custom_model', 'sharpness', 'extra_params', 'res_mode', 'custom_w', 'custom_h', 'interp', 'out_fps',
            'slowmo', 'duplicate', 'encoder', 'profile', 'compression', 'container', 'audio']
    return hashlib.sha1(json.dumps({k: s.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()[:12]


# ----------------------------------------------------------------------------- Topaz API client
class ApiError(Exception):
    def __init__(self, code, detail):
        super().__init__(f'HTTP {code}: {detail}')
        self.code = code


class Topaz:
    def __init__(self, base):
        self.base = base.rstrip('/')

    def call(self, key, method, path, body=None, tries=6):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(url, method=method, data=data, headers={
                'X-API-Key': key, 'User-Agent': UA, 'Accept': 'application/json', 'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    t = r.read().decode() or '{}'
                    return json.loads(t)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors='replace')[:400]
                if e.code == 429 or e.code >= 500:
                    wait = e.headers.get('Retry-After')
                    wait = float(wait) if wait and wait.replace('.', '', 1).isdigit() else min(60, 2 ** attempt)
                    time.sleep(wait + random.random())
                    continue
                raise ApiError(e.code, detail)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == tries - 1:
                    raise ApiError(0, f'network: {e}')
                time.sleep(min(30, 2 ** attempt))
        raise ApiError(429, 'still rate limited after retries')


def put_part(url, path, offset, length, on_bytes, tries=3):
    """Stream one part of the file to a presigned upload URL. Returns the ETag."""
    u = urllib.parse.urlsplit(url)
    target = u.path + ('?' + u.query if u.query else '')
    last = None
    for attempt in range(tries):
        sent = 0
        try:
            cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
            conn = cls(u.hostname, u.port, timeout=300)
            conn.putrequest('PUT', target, skip_accept_encoding=True)
            conn.putheader('Content-Type', 'video/mp4')
            conn.putheader('Content-Length', str(length))
            conn.endheaders()
            with open(path, 'rb') as f:
                f.seek(offset)
                left = length
                while left:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        raise IOError('file shrank during upload')
                    conn.send(chunk)
                    left -= len(chunk)
                    sent += len(chunk)
                    on_bytes(len(chunk))
            r = conn.getresponse()
            r.read()
            etag = r.getheader('ETag')
            conn.close()
            if r.status in (200, 201, 204) and etag:
                return etag
            last = f'upload HTTP {r.status}'
        except (OSError, http.client.HTTPException) as e:
            last = f'upload error: {e}'
        on_bytes(-sent)  # roll back progress for the retry
        time.sleep(2 ** attempt)
    raise RuntimeError(last)


# ----------------------------------------------------------------------------- app state
class App:
    def __init__(self, base, demo=False):
        self.api = Topaz(base)
        self.demo = demo
        self.lock = threading.RLock()
        self.key = os.environ.get('TOPAZ_API_KEY', '').strip()
        if not self.key and os.path.exists(KEY_FILE):
            try:
                self.key = open(KEY_FILE).read().strip()
            except OSError:
                pass
        self.settings = dict(DEFAULTS)
        if os.path.exists(SETTINGS_FILE):
            try:
                self.settings.update(json.load(open(SETTINGS_FILE)))
            except (OSError, ValueError):
                pass
        # v1 settings: 'fps' only applied with an engine picked; map so nothing silently retimes
        old_fps = self.settings.pop('fps', None)
        if self.settings.get('interp') == 'none':
            self.settings['interp'] = 'auto'
            self.settings['out_fps'] = 0.0
        elif old_fps and not self.settings.get('out_fps'):
            self.settings['out_fps'] = float(old_fps)
        if int(self.settings.get('v') or 0) < SETTINGS_V:
            self.settings['skip_existing'] = True   # v3: "only run what's left" is on for everyone after the update
            self.settings['v'] = SETTINGS_V
        self.jobs = []
        self.scan_info = {}
        self.logs = []
        self.running = False
        self.stop_flag = False    # no NEW clips start
        self.pause_flag = False   # user pressed stop: also stop waiting on clips at Topaz (they resume later)
        self.credit_stop = False
        self.workers = []
        self.fetching = False
        self.file_locks = {}      # one accept per file at a time, across workers
        self.token = secrets.token_hex(16)

    # -- key
    def set_key(self, key, remember):
        key = key.strip()
        if not key:
            raise ValueError('empty key')
        changed = key_fp(key) != key_fp(self.key)
        with self.lock:
            self.key = key
            if changed:   # free estimates belong to the old key's account: drop them, never accept them on the new one
                for j in self.jobs:
                    if j.get('request_id') and not j.get('accepted'):
                        j.update(request_id=None, estimate=None, settings_hash=None)
                        if j['stage'] == 'estimated':
                            j['stage'], j['msg'] = 'detected', 'key changed - estimate again if you want one'
                fp = key_fp(key)
                for j in self.jobs:   # back on the key that paid for it: resume it, never pay again
                    if j.get('hold') == 'other-account' and j.get('key_fp') == fp and j.get('request_id'):
                        mark_paid(j, j['request_id'], 'accepted')
                        j.update(stage='resumable', hold=None, selected=True,
                                 msg='this key paid for it - start or fetch finished resumes it, no new charge')
        if remember:
            os.makedirs(CONF_DIR, exist_ok=True)
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(key)
        elif os.path.exists(KEY_FILE):
            os.remove(KEY_FILE)   # otherwise the OLD remembered key would come back on the next launch
        log(('api key changed' if changed else 'api key set') +
            (' (remembered on this machine)' if remember else ' (this session only)'))

    def forget_key(self):
        with self.lock:
            self.key = ''
        if os.path.exists(KEY_FILE):
            os.remove(KEY_FILE)
        log('api key cleared - paste the new one' +
            (' (note: TOPAZ_API_KEY is set in your terminal, it comes back on restart)'
             if os.environ.get('TOPAZ_API_KEY') else ''))

    # -- helpers
    def add_log(self, msg):
        with self.lock:
            self.logs.append(time.strftime('%H:%M:%S ') + msg)
            self.logs = self.logs[-300:]

    def save_settings(self):
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(self.settings, f, indent=1)

    def set_job(self, job, **kw):
        with self.lock:
            job.update(kw)
            self.persist()

    def persist(self):
        out = self.settings.get('output_dir')
        if not out or not os.path.isdir(out):
            return
        keep = ['path', 'request_id', 'accepted', 'settings_hash', 'estimate', 'stage', 'out_name', 'error', 'key_fp',
                'slowmo_used', 'paid', 'hold', 'uploaded']
        sp = os.path.join(out, STATE_NAME)
        try:   # merge: entries for files not on the board right now are kept, so no paid id is ever forgotten
            data = json.load(open(sp)) if os.path.exists(sp) else {}
        except (OSError, ValueError):
            data = {}
        for j in self.jobs:
            if j.get('request_id') or j.get('paid'):
                data[j['path']] = {k: j.get(k) for k in keep}
        tmp = os.path.join(out, STATE_NAME + '.tmp')
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, os.path.join(out, STATE_NAME))

    def _plan(self, j):
        if not j.get('meta'):
            return None
        try:
            return fps_plan(j['meta'], self.settings)
        except ValueError as e:
            return {'error': str(e)}

    def public_state(self):
        with self.lock:
            return {
                'has_key': bool(self.key), 'key_hint': ('…' + self.key[-4:]) if len(self.key) > 8 else '',
                'demo': self.demo, 'settings': self.settings, 'running': self.running or self.fetching,
                'fetching': self.fetching, 'scan': self.scan_info,
                'jobs': [dict({k: v for k, v in j.items() if k != 'body'}, fps_plan=self._plan(j)) for j in self.jobs],
                'logs': self.logs[-80:],
                'options': {'upscale': UPSCALE_MODELS, 'interp': INTERP_MODELS, 'interp_hints': INTERP_HINTS,
                            'fps_presets': FPS_PRESETS, 'auto_engine': AUTO_ENGINE, 'encoders': ENCODERS},
            }

    # -- scanning
    def scan(self, inp, out):
        inp, out = os.path.expanduser(inp.strip()), os.path.expanduser(out.strip())
        if not os.path.isdir(inp):
            raise ValueError(f'input folder not found: {inp}')
        if not out:
            raise ValueError('pick an output folder')
        if os.path.abspath(inp) == os.path.abspath(out):
            raise ValueError('output folder must be different from the input folder')
        if self.running or self.fetching:
            raise ValueError('a batch is running - stop it before rescanning')
        os.makedirs(out, exist_ok=True)
        names = sorted(n for n in os.listdir(inp) if not n.startswith('.') and os.path.isfile(os.path.join(inp, n)))
        vids = [n for n in names if os.path.splitext(n)[1].lower().lstrip('.') in VIDEO_EXT]
        prev = {}
        sp = os.path.join(out, STATE_NAME)
        if os.path.exists(sp):
            try:
                prev = json.load(open(sp))
            except (OSError, ValueError):
                prev = {}
        idx = output_index(out)
        skip = bool(self.settings.get('skip_existing'))
        jobs = []
        for i, n in enumerate(vids):
            p = os.path.join(inp, n)
            job = {'id': str(i + 1), 'name': n, 'path': p, 'size': os.path.getsize(p),
                   'container': os.path.splitext(n)[1].lower().lstrip('.'), 'meta': None, 'stage': 'detected',
                   'pct': 0.0, 'msg': '', 'request_id': None, 'accepted': False, 'settings_hash': None,
                   'estimate': None, 'out_name': output_name(n, self.settings), 'error': None,
                   'started': None, 'finished': None, 'selected': True, 'partial': False, 'key_fp': None,
                   'slowmo_used': None, 'paid': [], 'hold': None, 'uploaded': None, 'rerun_ok': False}
            try:
                job['meta'] = probe(p)
            except Exception as e:  # noqa: BLE001 - show any probe failure on the board
                job['stage'], job['error'], job['selected'] = 'error', str(e), False
            old = prev.get(p) or {}
            job['paid'] = [dict(x) for x in (old.get('paid') or [])]
            if old.get('request_id') and old.get('accepted') and \
                    not any(x.get('id') == old['request_id'] for x in job['paid']):   # v2/v3 state: backfill
                job['paid'].append({'id': old['request_id'], 'key_fp': old.get('key_fp'),
                                    'status': 'complete' if old.get('stage') == 'done' else 'accepted'})
            found, fpath, why = (None, None, '')
            if skip and job['meta']:
                slowmos = {1.0, float(self.settings.get('slowmo') or 1), float(old.get('slowmo_used') or 1)}
                found, fpath, why = match_output(idx, n, job['meta'], tuple(slowmos))
            if found == 'done':
                rel = os.path.relpath(fpath, out)
                job.update(stage='skipped', selected=False, out_name=rel, out_path=fpath,
                           msg=f'done already: {rel}')
            elif old.get('hold') and not (old['hold'] == 'other-account' and old.get('request_id') and
                                          old.get('key_fp') == key_fp(self.key)):
                # paid before and needs the user's eyes: never picked up by a plain start
                job.update(request_id=old.get('request_id'), accepted=bool(old.get('accepted')),
                           settings_hash=old.get('settings_hash'), estimate=old.get('estimate'),
                           out_name=old.get('out_name') or job['out_name'], key_fp=old.get('key_fp'),
                           slowmo_used=old.get('slowmo_used'), uploaded=old.get('uploaded'), stage='held',
                           hold=old['hold'], selected=False, msg=HOLD_MSG.get(old['hold'], 'needs your ok'))
            elif old.get('request_id') and old.get('accepted') and old.get('stage') != 'done':
                # a paid job from an earlier session (an error after paying included): poll it, never pay twice
                job.update(request_id=old['request_id'], accepted=True, settings_hash=old.get('settings_hash'),
                           estimate=old.get('estimate'), out_name=old.get('out_name') or job['out_name'],
                           key_fp=old.get('key_fp'), slowmo_used=old.get('slowmo_used'), uploaded=old.get('uploaded'),
                           stage='resumable', msg='already paid at topaz - start resumes it, no new charge')
                mark_paid(job, old['request_id'], 'accepted')
            elif old.get('stage') == 'done' and old.get('request_id') and job['meta']:
                # paid + finished before, output gone from this folder: download again free, never re-run blind
                job.update(request_id=old['request_id'], accepted=True, out_name=old.get('out_name') or job['out_name'],
                           key_fp=old.get('key_fp'), slowmo_used=old.get('slowmo_used'), stage='held',
                           hold='done-missing', selected=False, msg=HOLD_MSG['done-missing'])
            elif found == 'partial':
                job.update(partial=True, msg=f'redo (partial output: {os.path.relpath(fpath, out)}, {why})')
            jobs.append(job)
        n_done = sum(j['stage'] == 'skipped' for j in jobs)
        n_part = sum(bool(j.get('partial')) for j in jobs)
        n_res = sum(j['stage'] == 'resumable' for j in jobs)
        n_held = sum(j['stage'] == 'held' for j in jobs)
        n_left = sum(j['stage'] in ('detected', 'resumable') and j.get('meta') is not None for j in jobs)
        summary = f'{n_done} done (skipped), {n_left} remaining, {n_part} partial (will redo)'
        if n_res:
            summary += f', {n_res} of the remaining already paid (resume, no new charge)'
        if n_held:
            summary += f', {n_held} held (paid before: fetch finished, or re-run needs your ok)'
        if not skip:
            summary = 'skip is OFF: every clip will run again. ' + summary
        with self.lock:
            self.settings['input_dir'], self.settings['output_dir'] = inp, out
            self.save_settings()
            self.jobs = jobs
            self.scan_info = {'input_dir': inp, 'output_dir': out, 'files': len(names), 'videos': len(vids),
                              'other_files': [n for n in names if n not in vids][:50], 'done': n_done,
                              'remaining': n_left, 'partial': n_part, 'resumable': n_res, 'held': n_held,
                              'summary': summary}
        log(f'scanned {inp}: {len(vids)} videos - {summary}')

    # -- settings
    def update_settings(self, new):
        with self.lock:
            for k, v in new.items():
                if k in DEFAULTS and k not in ('input_dir', 'output_dir'):
                    self.settings[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], bool) else bool(v)
            self.settings['parallel'] = max(1, min(10, int(self.settings['parallel'])))
            for j in self.jobs:
                if j['stage'] in ('detected', 'estimated'):
                    j['out_name'] = output_name(j['name'], self.settings)
            self.save_settings()

    def preview(self):
        with self.lock:
            job = next((j for j in self.jobs if j.get('meta')), None)
            if not job:
                return {'note': 'scan a folder to see the exact request body'}
            try:
                return {'clip': job['name'], 'body': build_body(job, self.settings)}
            except ValueError as e:
                return {'clip': job['name'], 'error': str(e)}

    # -- estimate (free: creates requests, never accepts them)
    def estimate(self, ids):
        if not self.key:
            raise ValueError('paste your API key first')
        targets = self._targets(ids, for_estimate=True)
        if not targets:
            raise ValueError('nothing to estimate')
        threading.Thread(target=self._estimate_all, args=(targets,), daemon=True).start()

    def _estimate_all(self, targets):
        h = settings_hash(self.settings)
        for job in targets:
            try:
                body = build_body(job, self.settings)
                self.set_job(job, stage='estimating', msg='', error=None)
                r = self.api.call(self.key, 'POST', '/', body)
                self.set_job(job, stage='estimated', request_id=r['requestId'], accepted=False, settings_hash=h,
                             estimate=r.get('estimates') or r.get('estimate'), msg='estimate only, not charged')
            except Exception as e:  # noqa: BLE001
                self.set_job(job, stage='error', error=str(e))
        log('estimates done')

    def _targets(self, ids, for_estimate=False):
        with self.lock:
            ok = ('detected', 'estimated', 'error') if for_estimate else \
                ('detected', 'estimated', 'error', 'resumable', 'held')
            return [j for j in self.jobs if j.get('meta') and j['stage'] in ok
                    and not (for_estimate and j.get('accepted'))
                    and not (j['stage'] == 'held' and not j.get('rerun_ok'))   # held clips only run after re-run ok
                    and (j['id'] in ids if ids else j.get('selected'))]

    @staticmethod
    def _resumes(j):
        """True = this clip already has a paid job at Topaz: start polls + downloads it, never pays again."""
        return j['stage'] == 'resumable' or (j['stage'] == 'error' and j.get('accepted') and j.get('request_id')
                                              and bool(live_paid(j)))

    def credit_plan(self, targets):
        new = [j for j in targets if not self._resumes(j)]
        lo = hi = 0
        unest = 0
        for j in new:
            e = j.get('estimate')
            c = e.get('cost') if isinstance(e, dict) else None
            if isinstance(c, list) and len(c) == 2:
                lo, hi = lo + c[0], hi + c[1]
            elif isinstance(c, (int, float)):
                lo, hi = lo + c, hi + c
            else:
                unest += 1
        return new, {'new': len(new), 'lo': lo, 'hi': hi, 'unestimated': unest, 'resume': len(targets) - len(new)}

    # -- run
    def start(self, ids, confirm=None):
        if not self.key:
            raise ValueError('paste your API key first')
        with self.lock:
            if self.running or self.fetching:
                raise ValueError('already running')
            targets = self._targets(ids)
            if not targets:
                raise ValueError('nothing queued - scan a folder or select clips')
            new, need = self.credit_plan(targets)
            for j in new:
                build_body(j, self.settings)  # fail fast on bad settings
            # anything that spends credits needs the user to confirm THIS total (a stale confirm is refused)
            if new and not (isinstance(confirm, dict) and all(confirm.get(k) == v for k, v in need.items())):
                raise NeedConfirm(need)
            for j in targets:
                if self._resumes(j):
                    j['stage'] = 'resumable'
                elif j['stage'] == 'held':   # the user hit "re-run (charges again)": old paid jobs written off on purpose
                    for p in live_paid(j):
                        p['status'] = 'superseded'
                    j.update(hold=None, request_id=None, accepted=False, rerun_ok=False, uploaded=None)
                elif j['stage'] == 'error':
                    j.update(request_id=None, accepted=False)   # never accepted: that request cost nothing
                j['error'] = None
                j['stage'], j['pct'], j['msg'] = ('queued' if j['stage'] != 'resumable' else 'resumable'), 0.0, ''
            self.persist()
            self.queue = list(targets)
            self.running, self.stop_flag, self.pause_flag, self.credit_stop = True, False, False, False
            n = min(self.settings['parallel'], len(targets))
            self.workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(n)]
        log(f'started {len(targets)} clip(s), {n} in parallel: {need["new"]} new (charged), {need["resume"]} '
            f'already paid (resume, free)')
        for t in self.workers:
            t.start()
        threading.Thread(target=self._watch, daemon=True).start()

    def rerun(self, jid):
        with self.lock:
            j = next((x for x in self.jobs if x['id'] == str(jid)), None)
            if not j or j['stage'] != 'held':
                raise ValueError('only held clips need a re-run ok')
            j.update(rerun_ok=True, selected=True, msg='re-run ok: the next start charges this clip again')
        log(f"{j['name']}: re-run approved (the next start charges it again)")

    def stop(self):
        with self.lock:
            self.stop_flag = self.pause_flag = True
            for j in self.jobs:
                if j['stage'] == 'queued':
                    j['stage'], j['msg'] = 'detected', 'removed from queue'
        log('stop: no new clips start; clips already paid keep processing at Topaz and show as resumable '
            '(start resumes them, no new charge). you can rescan as soon as the board says idle')

    def _watch(self):
        for t in self.workers:
            t.join()
        with self.lock:
            self.running = False
        log('batch finished')

    def _next(self):
        with self.lock:
            if self.stop_flag:
                return None
            while self.queue:
                j = self.queue.pop(0)
                if j['stage'] in ('queued', 'resumable'):
                    return j
            return None

    def _credit_stop(self):
        with self.lock:
            first = not self.credit_stop
            self.stop_flag = self.credit_stop = True
            for q in self.jobs:   # still waiting in the queue: back to remaining, nothing was spent
                if q['stage'] == 'queued':
                    q['stage'], q['msg'] = 'detected', 'not done: topaz is out of credits - top up or change key, then start'
        if first:
            log('OUT OF CREDITS: no new clips will start. top up or change the key, rescan, start: only what is left runs')

    def _worker(self):
        while True:
            job = self._next()
            if not job:
                return
            try:
                self._run_job(job)
            except OldKeyLost as e:
                mark_paid(job, job.get('request_id'), 'other-account')
                self.set_job(job, stage='held', hold='other-account', selected=False, error=None, finished=time.time(),
                             msg=HOLD_MSG['other-account'] + f' (HTTP {e})')
                log(f"{job['name']}: paid on the old key's account - held, NOT re-run (that would pay twice)")
            except DuplicateSubmit as e:
                self.set_job(job, stage='held', hold='dup', selected=False, error=str(e), finished=time.time(),
                             msg=HOLD_MSG['dup'])
                log(f"{job['name']}: blocked a second paid submit ({e})")
            except AcceptUnknown as e:
                self._settle_unknown(job, e)
            except Exception as e:  # noqa: BLE001
                if isinstance(e, TopazFailed):   # server-confirmed failed/canceled: topaz refunds it
                    mark_paid(job, job.get('request_id'), 'failed')
                    self.set_job(job, stage='held', hold='failed', request_id=None, accepted=False, selected=False,
                                 error=str(e), finished=time.time(), msg=HOLD_MSG['failed'])
                    log(f"{job['name']}: {e} - held, re-run needs your ok")
                    if is_credit_error(e):
                        self._credit_stop()
                elif is_credit_error(e) and not job.get('accepted'):
                    self._credit_stop()
                    # topaz refused the accept: nothing reserved, the unaccepted request is free to drop
                    self.set_job(job, stage='detected', request_id=None, accepted=False, key_fp=None,
                                 settings_hash=None, selected=True, error=None, finished=time.time(),
                                 msg='not done: topaz is out of credits - top up or change key, then start')
                else:   # accepted clips stay accepted: start (or a rescan) resumes them, never re-creates
                    self.set_job(job, stage='error', error=str(e), finished=time.time())
                    log(f"{job['name']}: {e}")

    def _settle_unknown(self, job, e):
        """Accept answer was lost, or the upload never finished. Ask Topaz what it has before doing anything."""
        rid = job.get('request_id')
        try:
            st = str(self.api.call(self.key, 'GET', f'/{rid}/status').get('status', '')).lower()
        except Exception:  # noqa: BLE001
            st = ''
        if st == 'requested':   # the accept never landed: nothing reserved
            mark_paid(job, rid, 'not-accepted')
            self.set_job(job, stage='error', accepted=False, error=None, finished=time.time(),
                         msg='accept did not go through, nothing charged - start tries again')
        elif st in ('', 'accepted'):
            self.set_job(job, stage='held', hold='no-upload', selected=False, error=str(e), finished=time.time(),
                         msg=HOLD_MSG['no-upload'])
            log(f"{job['name']}: accepted at topaz but not uploaded - held, NOT re-submitted")
        else:   # it is processing (or done) at topaz after all: just resume it
            mark_paid(job, rid, 'accepted')
            self.set_job(job, stage='error', uploaded=True, error=None, finished=time.time(),
                         msg='it is at topaz after all - start resumes it, no new charge')

    def _disk_live(self, path):
        sp = os.path.join(self.settings.get('output_dir') or '', STATE_NAME)
        try:
            d = (json.load(open(sp)) or {}).get(path) or {}
        except (OSError, ValueError):
            return []
        live = [x for x in (d.get('paid') or []) if x.get('status') in LIVE_PAID]
        if not d.get('paid') and d.get('accepted') and d.get('request_id') and d.get('stage') != 'done':
            live.append({'id': d['request_id']})   # v2/v3 state file
        return live

    def _run_job(self, job):
        s = dict(self.settings)
        out_dir = s['output_dir']
        job['started'] = time.time()
        if not (job['stage'] == 'resumable' and job.get('request_id')):
            with self.lock:
                lk = self.file_locks.setdefault(job['path'], threading.Lock())
            if not lk.acquire(blocking=False):
                raise DuplicateSubmit('another worker is submitting this file right now')
            try:
                # the one money rule: a file with a live paid job never gets a second accept
                live = live_paid(job) or self._disk_live(job['path'])
                if live:
                    raise DuplicateSubmit(f"already paid at topaz (job {str(live[-1].get('id'))[:8]})")
                h = settings_hash(s)
                if not (job.get('request_id') and not job.get('accepted') and job.get('settings_hash') == h):
                    self.set_job(job, stage='creating', msg='creating request')
                    r = self.api.call(self.key, 'POST', '/', build_body(job, s))
                    self.set_job(job, request_id=r['requestId'], settings_hash=h,
                                 estimate=r.get('estimates') or r.get('estimate'),
                                 out_name=output_name(job['name'], s))
                fp = key_fp(self.key)
                rid = job['request_id']
                self.set_job(job, key_fp=fp, slowmo_used=fps_plan(job['meta'], s)['slowmo'],
                             out_name=output_name(job['name'], s))
                self.set_job(job, stage='uploading', msg='accepting')
                try:
                    acc = self.api.call(self.key, 'PATCH', f'/{rid}/accept')
                except ApiError as e:
                    if e.code == 409 or e.code == 0 or e.code >= 500:   # may have gone through: never re-create
                        job['paid'].append({'id': rid, 'key_fp': fp, 'status': 'accept-unknown'})
                        self.set_job(job, accepted=True, uploaded=False)
                        raise AcceptUnknown(str(e))
                    raise   # a clean 4xx (402 included): nothing was reserved
                job['paid'].append({'id': rid, 'key_fp': fp, 'status': 'accepted'})
                self.set_job(job, accepted=True, uploaded=False)   # from here the job is billable
            finally:
                lk.release()
            urls = acc.get('urls') or []
            if not urls:
                raise RuntimeError(f'accept returned no upload urls: {str(acc)[:200]}')
            size = job['size']
            part = -(-size // len(urls))          # even split, every part <= 500 MB when the API sends N urls
            done = [0]

            def on_bytes(n):
                done[0] += n
                job['pct'] = round(100.0 * done[0] / size, 1)

            results = []
            for i, u in enumerate(urls):
                off = i * part
                ln = min(part, size - off)
                if ln <= 0:
                    break
                job['msg'] = f'uploading part {i + 1}/{len(urls)}'
                results.append({'partNum': i + 1, 'eTag': put_part(u, job['path'], off, ln, on_bytes)})
            self.api.call(self.key, 'PATCH', f"/{job['request_id']}/complete-upload/", {'uploadResults': results})
            self.set_job(job, uploaded=True, stage='upscaling', pct=0.0, msg='queued at Topaz')
        else:
            self.set_job(job, stage='upscaling', msg='resumed')
        # poll
        while True:
            if self.pause_flag:
                self.set_job(job, stage='resumable', msg='paused - still processing at topaz; start resumes it, '
                                                         'no new charge')
                return
            try:
                st = self.api.call(self.key, 'GET', f"/{job['request_id']}/status")
            except ApiError as e:
                # 403/404 with a different key than the one that submitted it = that job lives on the old account
                if e.code in (403, 404) and job.get('key_fp') != key_fp(self.key):
                    raise OldKeyLost(e.code)
                raise
            status = str(st.get('status', '')).lower()
            if status in ('requested', 'accepted') and job.get('uploaded') is False:
                raise AcceptUnknown('the upload never finished')
            prog = st.get('progress')
            job['pct'] = round(float(prog), 1) if isinstance(prog, (int, float)) else job['pct']
            job['msg'] = st.get('message') or status
            if status == 'complete':
                url = (st.get('download') or {}).get('url')
                if not url:
                    raise RuntimeError('complete but no download url')
                break
            if status in ('failed', 'canceled', 'cancelled'):
                raise TopazFailed(f"topaz {status}: {st.get('message') or st.get('error') or ''}".strip())
            for _ in range(max(3, int(s.get('poll_seconds') or 10))):
                if self.pause_flag:
                    break
                time.sleep(1)
        self._download(job, url, out_dir)

    def _download(self, job, url, out_dir):
        """Download, check, then rename. Only a complete, checked file ever gets the real name."""
        self.set_job(job, stage='downloading', pct=0.0, msg='')
        final = os.path.join(out_dir, job['out_name'])
        os.makedirs(os.path.dirname(final), exist_ok=True)
        tmp = final + '.part'
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    job['pct'] = round(100.0 * got / total, 1)
            f.flush()
            os.fsync(f.fileno())
        if not got or (total and got != total):
            os.remove(tmp)
            raise RuntimeError(f'download incomplete ({got}/{total or "?"} bytes) - start again to re-download, '
                               'no new charge')
        good, why = check_output(tmp, job['meta'], (1.0, float(job.get('slowmo_used') or 1)))
        if not good:
            os.remove(tmp)
            raise RuntimeError(f'downloaded file failed the completeness check ({why}) - start again to '
                               're-download, no new charge')
        os.replace(tmp, final)
        mark_paid(job, job.get('request_id'), 'complete')
        self.set_job(job, stage='done', pct=100.0, msg=f"renamed → {job['out_name']}", finished=time.time(),
                     out_path=final, hold=None)
        log(f"{job['name']} → {job['out_name']}")

    # -- fetch finished: download every paid job Topaz already finished, old runs included. Never pays.
    def fetch(self):
        if not self.key:
            raise ValueError('paste your API key first')
        with self.lock:
            if self.running or self.fetching:
                raise ValueError('busy - wait until the board says idle')
            todo = [j for j in self.jobs if j['stage'] not in ('done', 'skipped') and live_paid(j) and j.get('meta')]
            if not todo:
                raise ValueError('no paid jobs on this board to fetch')
            self.fetching = True
        threading.Thread(target=self._fetch_all, args=(todo,), daemon=True).start()

    def _fetch_all(self, todo):
        got = still = hidden = 0
        out_dir = self.settings['output_dir']
        try:
            for job in todo:
                seen = False
                for p in reversed(live_paid(job)):
                    try:
                        st = self.api.call(self.key, 'GET', f"/{p['id']}/status")
                    except ApiError as e:
                        if e.code in (401, 403, 404):
                            continue   # belongs to another account: needs that account's key
                        log(f"{job['name']}: fetch: {e}")
                        continue
                    seen = True
                    status = str(st.get('status', '')).lower()
                    if status in ('failed', 'canceled', 'cancelled'):
                        p['status'] = 'failed'
                        continue
                    if status == 'requested':
                        p['status'] = 'not-accepted'
                        continue
                    self.set_job(job, request_id=p['id'], accepted=True, key_fp=key_fp(self.key), hold=None)
                    if status == 'complete' and (st.get('download') or {}).get('url'):
                        try:
                            self._download(job, st['download']['url'], out_dir)
                            got += 1
                        except Exception as e:  # noqa: BLE001
                            self.set_job(job, stage='error', error=str(e))
                        break
                    p['status'] = 'accepted'
                    self.set_job(job, stage='resumable', selected=True, error=None,
                                 msg=f'still at topaz ({status}) - start resumes it, no new charge')
                    still += 1
                    break
                if not seen:
                    hidden += 1
                elif job['stage'] not in ('done', 'resumable', 'error') and not live_paid(job):
                    self.set_job(job, stage='held', hold='failed', request_id=None, accepted=False, selected=False,
                                 msg=HOLD_MSG['failed'])
                self.persist_safe()
        finally:
            with self.lock:
                self.fetching = False
        log(f'fetch finished: {got} downloaded (no charge), {still} still processing (start resumes them, no charge), '
            f'{hidden} not visible with this key (put the key that paid for them back, then fetch again)')

    def persist_safe(self):
        with self.lock:
            self.persist()


# ----------------------------------------------------------------------------- folder picker + open
def pick_folder(prompt):
    sysname = platform.system()
    try:
        if sysname == 'Darwin':
            p = subprocess.run(['osascript', '-e', f'POSIX path of (choose folder with prompt "{prompt}")'],
                               capture_output=True, text=True, timeout=600)
            return p.stdout.strip() or None
        if sysname == 'Windows':
            ps = ('Add-Type -AssemblyName System.Windows.Forms;$d=New-Object System.Windows.Forms.FolderBrowserDialog;'
                  f'$d.Description="{prompt}";if($d.ShowDialog() -eq "OK"){{$d.SelectedPath}}')
            p = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True,
                               timeout=600)
            return p.stdout.strip() or None
        if shutil.which('zenity'):
            p = subprocess.run(['zenity', '--file-selection', '--directory', f'--title={prompt}'],
                               capture_output=True, text=True, timeout=600)
            return p.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def open_folder(path):
    if not path or not os.path.isdir(path):
        raise ValueError('output folder not set')
    sysname = platform.system()
    if sysname == 'Darwin':
        subprocess.Popen(['open', path])
    elif sysname == 'Windows':
        os.startfile(path)  # noqa: S606 - local folder only
    else:
        subprocess.Popen(['xdg-open', path])


# ----------------------------------------------------------------------------- local web server
class Handler(BaseHTTPRequestHandler):
    server_version = 'TopazBatch/1.0'

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get('Host') or '').split(':')[0]
        return host in ('127.0.0.1', 'localhost')

    def _send(self, code, obj, ctype='application/json'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {'error': 'local only'})
        path = urllib.parse.urlsplit(self.path).path
        if path in ('/', '/index.html'):
            html = open(os.path.join(HERE, 'index.html'), encoding='utf-8').read().replace('{{TOKEN}}', APP.token)
            return self._send(200, html.encode(), 'text/html; charset=utf-8')
        if not self._auth():
            return
        if path == '/api/state':
            return self._send(200, APP.public_state())
        if path == '/api/preview':
            return self._send(200, APP.preview())
        self._send(404, {'error': 'not found'})

    def _auth(self):
        if self.headers.get('X-Batch-Token') != APP.token:
            self._send(403, {'error': 'bad token - reload the page'})
            return False
        return True

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {'error': 'local only'})
        if not self._auth():
            return
        path = urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        except ValueError:
            return self._send(400, {'error': 'bad json'})
        try:
            if path == '/api/key':
                APP.set_key(str(body.get('key', '')), bool(body.get('remember')))
            elif path == '/api/forget-key':
                APP.forget_key()
            elif path == '/api/settings':
                APP.update_settings(body)
            elif path == '/api/scan':
                APP.scan(body.get('input_dir', ''), body.get('output_dir', ''))
            elif path == '/api/pick':
                return self._send(200, {'path': pick_folder(str(body.get('prompt') or 'Pick a folder'))})
            elif path == '/api/select':
                with APP.lock:
                    for j in APP.jobs:
                        if j['id'] == str(body.get('id')):
                            j['selected'] = bool(body.get('selected'))
            elif path == '/api/estimate':
                APP.estimate(body.get('ids') or [])
            elif path == '/api/start':
                APP.start(body.get('ids') or [], body.get('confirm'))
            elif path == '/api/rerun':
                APP.rerun(body.get('id'))
            elif path == '/api/fetch':
                APP.fetch()
            elif path == '/api/stop':
                APP.stop()
            elif path == '/api/open':
                open_folder(APP.settings.get('output_dir'))
            else:
                return self._send(404, {'error': 'not found'})
        except NeedConfirm as e:
            return self._send(409, {'error': str(e), 'need_confirm': e.info})
        except (ValueError, ApiError, RuntimeError, OSError) as e:
            return self._send(400, {'error': str(e)})
        self._send(200, {'ok': True})


APP = None


def main():
    global APP
    ap = argparse.ArgumentParser(description='Topaz batch upscaler (local)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--demo', action='store_true', help='fake local Topaz API: try the whole flow, no key, no credits')
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    base = os.environ.get('TOPAZ_API_BASE') or TOPAZ_BASE   # override only used for testing
    if a.demo:
        import mock_topaz
        base = mock_topaz.start_background()
    APP = App(base, demo=a.demo)
    if a.demo and not APP.key:
        APP.key = 'demo-key'
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    url = f'http://127.0.0.1:{a.port}'
    print(f'Topaz Batch running at {url}' + ('  [DEMO MODE: fake API, no credits]' if a.demo else ''))
    print('Ctrl+C to quit. Jobs already at Topaz keep running; start the app again and rescan to resume them.')
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')


if __name__ == '__main__':
    sys.path.insert(0, HERE)
    main()
