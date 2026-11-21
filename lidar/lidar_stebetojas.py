#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEO GROUP – LiDAR užsakymų stebėtojas (veikia fone).

  • Priima užsakymus iš žemėlapio mygtuko „LiDAR reljefas“ (tik iš šio kompiuterio, adresu 127.0.0.1).
  • Taip pat stebi aplanką „Atsisiuntimai“: jei ten atsiranda GeoGroup_LiDAR_uzsakymas*.json – jį atlieka.
  • Užsakymus atlieka po vieną su lidar_reljefas.py; baigus atidaro rezultatų aplanką.
  • Žemėlapis gali klausti būsenos (eiga rodoma kortelėje).

Paleidžiama automatiškai prisijungus prie Windows (žr. AUTOMATINIS_PALEIDIMAS.bat).
Sustabdyti: STEBETOJAS_ISJUNGTI.bat (arba sukurti failą STOP šiame aplanke).
"""
import glob, http.server, json, os, queue, re, shutil, socket, subprocess, sys, threading, time, datetime

VERSION = 'lidar_stebetojas 1.2 (2026-11-21)'
PORT = 47391
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'lidar_reljefas.py')
JOBS_DIR = os.path.join(HERE, 'uzsakymai')
LOG = os.path.join(HERE, 'stebetojas.log')
CONF = os.path.join(HERE, 'stebetojas_nustatymai.json')
PATTERN = 'GeoGroup_LiDAR_uzsakymas*.json'

Q = queue.Queue()
STATE = {'versija': VERSION, 'dabar': None, 'eile': [], 'paskutiniai': [], 'pradzia': datetime.datetime.now().isoformat(timespec='seconds')}
LOCK = threading.Lock()


def log(msg):
    line = '%s  %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        if os.path.getsize(LOG) > 2e6:
            shutil.move(LOG, LOG + '.senas')
    except Exception:
        pass


def watch_dirs():
    dirs = [os.path.join(os.path.expanduser('~'), 'Downloads')]
    try:
        dirs += json.load(open(CONF, encoding='utf-8')).get('papildomi_aplankai', [])
    except Exception:
        pass
    return [d for d in dirs if os.path.isdir(d)]


def python_exe():
    exe = sys.executable
    if os.path.basename(exe).lower() == 'pythonw.exe':
        cand = os.path.join(os.path.dirname(exe), 'python.exe')
        if os.path.exists(cand):
            return cand
    return exe


def job_cmd(path):
    """Komanda vienam užsakymui atlikti (.exe versija šią funkciją pakeičia)."""
    return [python_exe(), SCRIPT, path, '--atidaryti']


def valid_order(js):
    if not isinstance(js, dict) or not js.get('geogroup_lidar_uzsakymas'):
        return 'ne užsakymo failas'
    if not js.get('ribos') and not js.get('kadastro_nr'):
        return 'nėra ribų'
    p = js.get('parametrai', {}) or {}
    for k, lo, hi in (('intervalas', 0.05, 20), ('taskai', 0, 100), ('buferis', 0, 200), ('gardele', 0.25, 5)):
        if k in p:
            try:
                v = float(p[k])
            except Exception:
                return 'blogas parametras %s' % k
            if not (lo <= v <= hi):
                return 'parametras %s už ribų' % k
    rings = js.get('ribos') or []
    if sum(len(r) for r in rings) > 20000:
        return 'per daug ribos taškų'
    return None


def add_order(js, source):
    err = valid_order(js)
    if err:
        return None, err
    os.makedirs(JOBS_DIR, exist_ok=True)
    name = re.sub(r'[^0-9A-Za-z_.-]+', '_', str(js.get('pavadinimas') or 'plotas'))[:60]
    jid = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_') + name
    path = os.path.join(JOBS_DIR, jid + '.json')
    json.dump(js, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    job = {'id': jid, 'pavadinimas': js.get('pavadinimas') or name, 'failas': path, 'saltinis': source, 'busena': 'eilėje',
           'eiga': [], 'pateikta': datetime.datetime.now().isoformat(timespec='seconds')}
    with LOCK:
        STATE['eile'].append(job)
    Q.put(job)
    log('Naujas užsakymas %s (%s)' % (jid, source))
    return job, None


def run_jobs():
    while True:
        job = Q.get()
        with LOCK:
            if job in STATE['eile']:
                STATE['eile'].remove(job)
            STATE['dabar'] = job
            job['busena'] = 'vykdoma'; job['pradzia'] = time.time()
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        cmd = job_cmd(job['failas'])
        flags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, creationflags=flags,
                                 encoding='utf-8', errors='replace', cwd=HERE)
            for line in p.stdout:
                line = line.rstrip()
                if not line:
                    continue
                with LOCK:
                    job['eiga'].append(line); job['eiga'] = job['eiga'][-60:]
                    m = re.search(r'Rezultatai: (.+)$', line)
                    if m:
                        job['rezultatai'] = m.group(1).strip()
            rc = p.wait()
            ok = rc == 0
        except Exception as e:
            ok = False
            with LOCK:
                job['eiga'].append('KLAIDA paleidžiant: %s' % e)
        with LOCK:
            job['busena'] = 'baigta' if ok else 'klaida'
            job['trukme_s'] = round(time.time() - job['pradzia'])
            STATE['dabar'] = None
            STATE['paskutiniai'] = ([job] + STATE['paskutiniai'])[:10]
        log('Užsakymas %s – %s (%d s)' % (job['id'], job['busena'], job['trukme_s']))
        if not ok:
            notify('LiDAR užsakymas nepavyko: %s' % job['pavadinimas'], '\n'.join(job['eiga'][-6:]))
        Q.task_done()


def notify(title, text):
    if os.name != 'nt':
        return
    try:
        import ctypes
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(0, text, title, 0x40030), daemon=True).start()
    except Exception:
        pass


def folder_watch():
    seen = {}
    while True:
        if os.path.exists(os.path.join(HERE, 'STOP')):
            log('Rastas STOP failas – stabdoma'); os._exit(0)
        for d in watch_dirs():
            for f in glob.glob(os.path.join(d, PATTERN)):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                key = (f, st.st_size, st.st_mtime)
                if seen.get(f) != key:
                    seen[f] = key; continue  # palaukiame, kol failas nebesikeis
                try:
                    js = json.load(open(f, encoding='utf-8-sig'))
                except Exception:
                    continue
                job, err = add_order(js, 'aplankas: ' + os.path.basename(f))
                done = os.path.join(JOBS_DIR, 'perkelti'); os.makedirs(done, exist_ok=True)
                try:
                    shutil.move(f, os.path.join(done, datetime.datetime.now().strftime('%Y%m%d_%H%M%S_') + os.path.basename(f)))
                except Exception:
                    pass
                seen.pop(f, None)
                if err:
                    log('Atmestas %s: %s' % (f, err))
        time.sleep(3)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    ALLOWED = ('https://uabgeogroup-svg.github.io',)

    def _origin_ok(self):
        o = self.headers.get('Origin')
        return o in (None, 'null') or (o or '').startswith('file://') or o in self.ALLOWED

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', (self.headers.get('Origin') or 'null') if self._origin_ok() else 'http://neleidziama')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_OPTIONS(self):
        self._send(204 if self._origin_ok() else 403, {})

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {'klaida': 'neleidžiama'})
        if self.path.startswith('/busena'):
            with LOCK:
                s = json.loads(json.dumps(STATE, default=str))
            return self._send(200, s)
        self._send(404, {'klaida': 'nėra'})

    def do_POST(self):
        if not self._origin_ok():
            return self._send(403, {'klaida': 'neleidžiama'})
        if self.path != '/uzsakymas':
            return self._send(404, {'klaida': 'nėra'})
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > 2_000_000:
            return self._send(400, {'klaida': 'blogas dydis'})
        try:
            js = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return self._send(400, {'klaida': 'blogas JSON'})
        job, err = add_order(js, 'žemėlapis')
        if err:
            return self._send(400, {'klaida': err})
        return self._send(200, {'id': job['id'], 'eileje_pries': Q.qsize() - 1})


def main():
    try:
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), H)
    except OSError:
        sys.exit(0)  # jau veikia kitas egzempliorius
    if os.path.exists(os.path.join(HERE, 'STOP')):
        os.remove(os.path.join(HERE, 'STOP'))
    log('Paleistas %s, stebimi aplankai: %s' % (VERSION, '; '.join(watch_dirs())))
    threading.Thread(target=run_jobs, daemon=True).start()
    threading.Thread(target=folder_watch, daemon=True).start()
    srv.serve_forever()


if __name__ == '__main__':
    main()
