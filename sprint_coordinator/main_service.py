"""Persistent, model-free ingress for a human-authorized main agent session.

This supervises sprint-dispatch, not a task worker. It does not change ownership,
advance a cursor, reset delivery state, or select a worker provider.
"""
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

from sprint_coordinator.board import BoardClient

ROOT = Path(__file__).resolve().parents[1]


def identity(project):
    data = Path(project).resolve() / '.sprint'
    label = 'day.sprint.dispatch.' + hashlib.sha256(str(data).encode()).hexdigest()[:12]
    return data, label


def dispatcher():
    loader = SourceFileLoader('sprint_main_dispatch', str(ROOT / 'bin/sprint-dispatch'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def preflight(project, board=None):
    data, _ = identity(project)
    board = board or BoardClient(data)
    settings = board.settings()
    target = settings.get('session_tmux_window', settings.get('settings', {}).get('session_tmux_window'))
    if not isinstance(target, str) or not re.fullmatch(r'%[0-9]+', target):
        raise RuntimeError('register an exact main-session pane before starting ingress')
    health = board.autoheal()
    if not health.get('event_dispatch_supported'):
        raise RuntimeError('board needs event-dispatch support before starting ingress')
    if health.get('coordinator'):
        raise RuntimeError('the task coordinator already owns ingress; stop it before switching modes')
    if health.get('tmux_window') != target:
        raise RuntimeError('board owner changed during preflight')
    dispatch = dispatcher()
    saved = dispatch.read_json(data / 'dispatch.json', {})
    if saved and saved.get('target') != target:
        raise RuntimeError('finish the acknowledged owner handoff before starting ingress')
    return target


def plist_value(project):
    data, label = identity(project)
    return {
        'Label': label,
        'ProgramArguments': [sys.executable, str(ROOT / 'bin/sprint-dispatch-service'),
                             'run', '--project-root', str(Path(project).resolve())],
        'WorkingDirectory': str(ROOT), 'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 30,
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:' + str(Path.home() / '.local/bin')},
        'StandardOutPath': str(data / 'dispatch-service.log'),
        'StandardErrorPath': str(data / 'dispatch-service.log'),
    }


def run(project):
    # Re-read the registered owner on every supervisor restart. The handoff must
    # already have reconciled durable delivery state; never hide it with --reset.
    target = preflight(project)
    argv = [sys.executable, str(ROOT / 'bin/sprint-dispatch'), 'run',
            '--project-root', str(Path(project).resolve()), '--target', target]
    os.execv(sys.executable, argv)


def service(action, project):
    data, label = identity(project)
    domain = 'gui/' + str(os.getuid())
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    if action == 'status':
        result = subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True)
        return {'installed': path.exists(), 'supervised': result.returncode == 0,
                'dispatch': dispatcher().status(data)}
    if action == 'uninstall':
        subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
        path.unlink(missing_ok=True)
        return {'uninstalled': label}
    preflight(project)
    current = dispatcher().status(data)
    if current['running']:
        # Reinstalling a healthy service is unnecessary. A standalone dispatcher
        # must be explicitly stopped first, preserving its saved delivery state.
        check = subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True)
        pid = re.search(rb'^\s*pid = ([0-9]+)\s*$', check.stdout, re.M)
        if check.returncode == 0 and pid and int(pid[1]) == current.get('pid'):
            return {'installed': label, 'already_running': True}
        raise RuntimeError('stop the standalone dispatcher before installing its supervisor')
    data.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'wb') as out:
        os.chmod(path, 0o600)
        plistlib.dump(plist_value(project), out)
    subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
    return {'installed': label}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'uninstall', 'status', 'run'])
    parser.add_argument('--project-root', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'run':
            run(args.project_root)
        else:
            print(json.dumps(service(args.action, args.project_root)))
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print('sprint-dispatch-service: ' + str(exc), file=sys.stderr)
        return 1
