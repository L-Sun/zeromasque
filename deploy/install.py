#!/usr/bin/env python3
"""Install only zeromasque.service, with backup and a 15-minute rollback guard."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

SERVICE = 'zeromasque.service'


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def atomic(path, data, mode=0o600):
    tmp = path.with_name(path.name + '.new')
    with tmp.open('x') as f:
        f.write(data)
    tmp.chmod(mode)
    tmp.replace(path)


def rollback(state):
    drop = pathlib.Path(state['dropin'])
    if state['previous_dropin'] is None:
        drop.unlink(missing_ok=True)
    else:
        atomic(drop, state['previous_dropin'])
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'restart', SERVICE], check=True)
    atomic(pathlib.Path(state['state_dir']) / 'status', 'rolled_back\n')


def guard(statefile):
    state = json.loads(pathlib.Path(statefile).read_text())
    status = pathlib.Path(state['state_dir']) / 'status'

    def interrupt(sig, frame):
        raise SystemExit(128 + sig)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupt)
    committed = False
    try:
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if pathlib.Path(state['commit_file']).read_text().strip() == state['sha256']:
                committed = True
                atomic(status, 'committed\n')
                return
            time.sleep(1)
    finally:
        if not committed:
            rollback(state)
        pathlib.Path(state['commit_file']).unlink(missing_ok=True)


def systemd_quote(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$').replace('\n', '\\n') + '"'


def install(binary, expected, original_sha, expected_mode, commit_uid):
    if os.geteuid() != 0:
        raise RuntimeError('Run this installer with sudo')
    data = pathlib.Path(binary).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError('Staged build checksum differs')
    original = pathlib.Path('/usr/local/bin/zeromasque')
    if hashlib.sha256(original.read_bytes()).hexdigest() != original_sha:
        raise RuntimeError('Original binary changed; inspect before deploying')
    pid = run('systemctl', 'show', SERVICE, '-p', 'MainPID', '--value')
    args = [s.decode() for s in pathlib.Path('/proc/' + pid + '/cmdline').read_bytes().split(b'\0') if s]
    if len(args) < 2 or args[0] != str(original) or args[1] != expected_mode:
        raise RuntimeError('Unexpected running service; refusing duplicate deployment')
    suffix = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + expected[:8]
    state_dir = pathlib.Path('/var/lib/zeromasque-bbrfix') / suffix
    state_dir.mkdir(parents=True, mode=0o700)
    shutil.copy2(original, state_dir / 'original-binary')
    destination = pathlib.Path('/usr/local/libexec') / ('zeromasque-bbrfix-' + expected[:16])
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Existing versioned binary checksum differs')
    else:
        with destination.open('xb') as f:
            f.write(data)
        destination.chmod(0o755)
    if 'bbrfix' not in run(str(destination), '--version'):
        raise RuntimeError('Build version missing fix identifier')
    drop = pathlib.Path('/etc/systemd/system/' + SERVICE + '.d/90-codex-bbrfix.conf')
    drop.parent.mkdir(parents=True, exist_ok=True)
    previous = drop.read_text() if drop.exists() else None
    commit = pathlib.Path('/run/zeromasque-bbrfix-' + suffix + '.commit')
    with commit.open('x') as f:
        f.write('')
    commit.chmod(0o600)
    os.chown(commit, commit_uid, -1)
    state = {'state_dir': str(state_dir), 'dropin': str(drop), 'previous_dropin': previous,
             'sha256': expected, 'original_sha256': original_sha, 'installed_binary': str(destination),
             'commit_file': str(commit), 'guard_unit': 'zeromasque-bbrfix-guard-' + suffix}
    atomic(state_dir / 'state.json', json.dumps(state))
    shutil.copy2(__file__, state_dir / 'installer.py')
    (state_dir / 'installer.py').chmod(0o700)
    # Guard survives SSH loss. Killing/stopping the guard also triggers rollback.
    subprocess.run(['systemd-run', '--quiet', '--unit=' + state['guard_unit'],
                    '/usr/bin/python3', str(state_dir / 'installer.py'), 'guard',
                    str(state_dir / 'state.json')], check=True)
    args[0] = str(destination)
    try:
        atomic(drop, '[Service]\nExecStart=\nExecStart=' + ' '.join(map(systemd_quote, args)) + '\n')
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', SERVICE], check=True)
        if run('systemctl', 'is-active', SERVICE) != 'active':
            raise RuntimeError('Service not active after restart')
        atomic(state_dir / 'status', 'pending_validation\n')
        # Public receipt contains no service arguments, endpoint credentials or keys.
        receipt = {k: state[k] for k in ('sha256', 'original_sha256', 'installed_binary',
                                        'commit_file', 'guard_unit', 'state_dir')}
        atomic(pathlib.Path('/run/zeromasque-bbrfix-latest.json'), json.dumps(receipt), 0o644)
        print('DEPLOY_READY ' + json.dumps(receipt), flush=True)
    except BaseException:
        subprocess.run(['systemctl', 'stop', state['guard_unit'] + '.service'])
        raise


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'guard':
        guard(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == 'rollback':
        if os.geteuid() != 0:
            raise RuntimeError('Run rollback with sudo')
        state = json.loads(pathlib.Path(sys.argv[2]).read_text())
        if subprocess.run(['systemctl', 'is-active', '--quiet', state['guard_unit'] + '.service']).returncode == 0:
            subprocess.run(['systemctl', 'stop', state['guard_unit'] + '.service'], check=True)
        else:
            rollback(state)
    else:
        p = argparse.ArgumentParser()
        p.add_argument('binary')
        p.add_argument('sha256')
        p.add_argument('original_sha256')
        p.add_argument('mode', choices=['serve', 'client'])
        p.add_argument('--commit-uid', type=int, default=1000)
        a = p.parse_args()
        install(a.binary, a.sha256, a.original_sha256, a.mode, a.commit_uid)
