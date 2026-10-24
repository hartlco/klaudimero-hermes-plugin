import uuid
from types import SimpleNamespace
import pytest
from klaudimero_plugin import lifecycle
from klaudimero_plugin.contracts import Problem

BASE = '/api/plugins/klaudimero'
INSTANCE = 'a' * 64
COMMAND = ['/usr/bin/systemctl', '--user', '--no-ask-password', '--no-block', 'restart', 'hermes-dashboard.service']


def test_restart_auth_consent_identity_and_idempotency(client, monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle, '_target', lambda: (INSTANCE, COMMAND))
    monkeypatch.setattr(lifecycle.subprocess, 'run', lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    assert client.get(BASE+'/lifecycle').json()['restart_available'] is True
    payload = {'request_id': str(uuid.uuid4()), 'expected_instance': INSTANCE, 'interrupt': True}
    client.headers.pop('Authorization')
    assert client.post(BASE+'/lifecycle/dashboard/restart', json=payload).status_code == 401
    client.headers['Authorization'] = 'Bearer alice'
    assert client.post(BASE+'/lifecycle/dashboard/restart', json={**payload, 'interrupt': False}).status_code == 409
    assert client.post(BASE+'/lifecycle/dashboard/restart', json={**payload, 'expected_instance': 'b'*64}).status_code == 409
    assert not calls
    first = client.post(BASE+'/lifecycle/dashboard/restart', json=payload)
    assert first.json()['state'] == 'requested'
    assert client.post(BASE+'/lifecycle/dashboard/restart', json=payload).json() == first.json()
    assert len(calls) == 1 and calls[0] == COMMAND
    assert client.post(BASE+'/lifecycle/dashboard/restart', json={**payload, 'request_id': str(uuid.uuid4())}).status_code == 409
    client.headers['Authorization'] = 'Bearer bob'
    assert client.post(BASE+'/lifecycle/dashboard/restart', json=payload).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize('outcome', ['denied', 'uncertain'])
def test_restart_failed_and_uncertain_are_never_replayed(client, monkeypatch, outcome):
    calls = []
    monkeypatch.setattr(lifecycle, '_target', lambda: (INSTANCE, COMMAND))
    def run(argv, **kw):
        calls.append(argv)
        if outcome == 'uncertain': raise lifecycle.subprocess.TimeoutExpired(argv, 5)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(lifecycle.subprocess, 'run', run)
    payload = {'request_id': str(uuid.uuid4()), 'expected_instance': INSTANCE, 'interrupt': True}
    first = client.post(BASE+'/lifecycle/dashboard/restart', json=payload)
    assert first.json()['state'] == ('failed' if outcome == 'denied' else 'requested')
    assert client.post(BASE+'/lifecycle/dashboard/restart', json=payload).json() == first.json()
    assert len(calls) == 1


def test_unmanaged_dashboard_does_not_run_commands(client, monkeypatch):
    monkeypatch.setattr(lifecycle, '_target', lambda: (INSTANCE, None))
    assert not client.get(BASE+'/lifecycle').json()['restart_available']
    assert client.post(BASE+'/lifecycle/dashboard/restart', json={'request_id': str(uuid.uuid4()),
        'expected_instance': INSTANCE, 'interrupt': True}).status_code == 409


@pytest.mark.parametrize('path,main_pid,allowed', [
    ('/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-dashboard.service', 42, True),
    ('/system.slice/hermes-dashboard.service', 42, True),
    ('/system.slice/docker.service', 1, False),
    ('/system.slice/cron.service', 90, False),
    ('/user.slice/user-1000.slice/user@1000.service', 42, False),
    ('/system.slice/app.scope', 42, False),
])
def test_only_exact_dashboard_main_pid_can_be_restarted(monkeypatch, path, main_pid, allowed):
    monkeypatch.setattr(lifecycle, '_dashboard', lambda: (42, INSTANCE))
    monkeypatch.setattr(lifecycle, '_read', lambda pid, name: ('0::'+path+'\n').encode())
    monkeypatch.setattr(lifecycle.shutil, 'which', lambda name: '/usr/bin/systemctl')
    monkeypatch.setattr(lifecycle.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=str(main_pid).encode()))
    instance, command = lifecycle._target()
    assert instance == INSTANCE
    assert (command is not None) == allowed
    if allowed:
        assert command[-3:] == ['--no-block', 'restart', 'hermes-dashboard.service']
        assert ('--user' in command) == path.startswith('/user.slice/')


def test_plugin_host_walks_to_dashboard_but_rejects_shell_and_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle.sys, 'platform', 'linux')
    monkeypatch.setattr(lifecycle.os, 'getpid', lambda: 43)
    real_read = lifecycle.Path.read_text
    monkeypatch.setattr(lifecycle.Path, 'read_text', lambda path, *a, **kw: 'boot' if str(path).endswith('boot_id') else real_read(path, *a, **kw))
    def read(pid, name):
        if name == 'stat': return (f'{pid} (python) S '+('42' if pid == 43 else '1')+' '+'0 '*17+'1234 0').encode()
        if pid == 43: return b'python\0-m\0hermes_cli.plugin_host_child\0'
        return b'python\0/home/martin/hermes\0-p\0default\0dashboard\0--port\09119\0'
    monkeypatch.setattr(lifecycle, '_read', read)
    assert lifecycle._dashboard()[0] == 42
    original = read
    monkeypatch.setattr(lifecycle, '_read', lambda pid, name: b'/bin/sh\0-c\0hermes dashboard\0' if name == 'cmdline' and pid == 42 else original(pid, name))
    assert lifecycle._dashboard() is None
