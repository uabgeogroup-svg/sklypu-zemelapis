# -*- coding: utf-8 -*-
"""
GEO GROUP – LiDAR reljefas (vienas .exe failas, Python nereikia).

  • Paleidus du kartus spragtelėjus: įjungiamas stebėtojas (veikia fone, pasileidžia su Windows)
    ir galima iškart įvesti kadastro Nr.
  • Užtempus ant .exe DXF / GeoJSON / užsakymo .json failą – reljefas pagal tą failą.
  • Žemėlapio mygtukas „LiDAR reljefas“ užsakymus siunčia stebėtojui automatiškai.
"""
import os, subprocess, sys, time, urllib.request

FROZEN = getattr(sys, 'frozen', False)
EXE = sys.executable if FROZEN else os.path.abspath(__file__)
DATA = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'GeoGroupLiDAR')
VBS = os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup', 'GeoGroup_LiDAR_exe.vbs')
EXE_VERSION = 'GeoGroup_LiDAR.exe 1.0 (2026-11-21)'

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def self_cmd(*args):
    return [sys.executable, *args] if FROZEN else [sys.executable, EXE, *args]


def watcher_alive():
    try:
        with urllib.request.urlopen('http://127.0.0.1:47391/busena', timeout=2) as r:
            import json
            return json.loads(r.read().decode('utf-8')).get('versija', 'veikia')
    except Exception:
        return None


def pause():
    try:
        input('\nSpauskite Enter, kad uždarytumėte langą...')
    except Exception:
        pass


def run_watcher():
    import lidar_stebetojas as W
    os.makedirs(DATA, exist_ok=True)
    W.HERE = DATA
    W.JOBS_DIR = os.path.join(DATA, 'uzsakymai')
    W.LOG = os.path.join(DATA, 'stebetojas.log')
    W.CONF = os.path.join(DATA, 'stebetojas_nustatymai.json')
    W.STATE['versija'] = W.VERSION + ' / ' + EXE_VERSION
    W.job_cmd = lambda path: self_cmd(path, '--atidaryti', '--be-pauzes')
    W.main()


def run_relief(argv):
    import lidar_reljefas as R
    nopause = '--be-pauzes' in argv
    argv = [a for a in argv if a != '--be-pauzes']
    code = 1
    try:
        code = R.main(argv) or 0
    except R.Klaida as e:
        R.log('\nKLAIDA: %s' % e); code = 2
    except KeyboardInterrupt:
        R.log('\nNutraukta. Paleidus iš naujo, parsisiuntimas bus tęsiamas.'); code = 3
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        R.log('\nNETIKĖTA KLAIDA: %r' % (e,)); code = 4
    if not nopause:
        pause()
    return code


def install_and_start():
    print('GEO GROUP – LiDAR reljefas')
    print(EXE_VERSION)
    print('=' * 60)
    # 1) automatinis paleidimas su Windows
    try:
        os.makedirs(os.path.dirname(VBS), exist_ok=True)
        with open(VBS, 'w', encoding='utf-16') as f:
            f.write('CreateObject("WScript.Shell").Run """%s"" --stebetojas", 0, False\r\n' % EXE)
        print('Automatinis paleidimas įjungtas (kartu su Windows).')
    except Exception as e:
        print('Nepavyko įjungti automatinio paleidimo: %s' % e)
    # 2) stebėtojas
    v = watcher_alive()
    if not v:
        try:
            if os.path.exists(VBS):
                os.startfile(VBS)
            else:
                subprocess.Popen(self_cmd('--stebetojas'), creationflags=0x08000000)
        except Exception as e:
            print('Nepavyko paleisti stebėtojo: %s' % e)
        for _ in range(15):
            time.sleep(1)
            v = watcher_alive()
            if v:
                break
    if v:
        print('Stebėtojas veikia: %s' % v)
        print('Dabar žemėlapio mygtukas „LiDAR reljefas“ veiks automatiškai.')
    else:
        print('DĖMESIO: stebėtojas neatsako. Pabandykite paleisti šį failą dar kartą.')
    print()
    print('Galite ir iškart gauti reljefą pagal kadastro Nr.')
    while True:
        try:
            s = input('Įveskite kadastro Nr. (kelis – per tarpą), arba Enter – išeiti: ').strip()
        except Exception:
            return 0
        if not s:
            return 0
        run_relief(['--kad', *s.replace(',', ' ').split(), '--atidaryti', '--be-pauzes'])
        print()


def main():
    args = sys.argv[1:]
    if args[:1] == ['--stebetojas']:
        return run_watcher()
    if args[:1] in (['--versija'], ['--version']):
        import lidar_reljefas as R, lidar_stebetojas as W
        print(EXE_VERSION); print(R.VERSION); print(W.VERSION)
        return 0
    if args[:1] in (['--diegti'], []):
        return install_and_start()
    if args[:1] == ['--isjungti']:
        os.makedirs(DATA, exist_ok=True)
        open(os.path.join(DATA, 'STOP'), 'w').close()
        try:
            os.remove(VBS)
        except OSError:
            pass
        print('Stebėtojas bus sustabdytas per kelias sekundes, automatinis paleidimas išjungtas.')
        return 0
    return run_relief(args)


if __name__ == '__main__':
    sys.exit(main())
