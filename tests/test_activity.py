import base64
import json
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from klaudimero_plugin.contracts import Problem
from klaudimero_plugin.storage import Store

BASE = '/api/plugins/klaudimero'


def enroll(store, owner='alice', platform='ios'):
    device = str(uuid.uuid4())
    key = base64.b64encode(X25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    store.register_device(owner, device, 'b' * 64, 'production', [{'profile_id': 'japan', 'session_id': 'bot'}], key, platform=platform)
    return device


def test_foreground_mac_quiets_only_its_owners_mobile_devices(tmp_path):
    store = Store(tmp_path)
    phone, legacy, mac, other = enroll(store), enroll(store, platform='unknown'), enroll(store, platform='macos'), enroll(store, owner='bob')
    store.mac_activity('alice', str(uuid.uuid4()), 1, True)
    store.enqueue('japan', 'bot', 'turn1', 'reply_ready', notification={'title': 'Japan', 'body': 'Encrypted answer'})
    delivered = []
    while row := store.claim():
        delivered.append(json.loads(row['payload'])['device_token'])
        store.settle(row['id'])
    with store.connection() as db:
        live = {r['device']: r['payload'] for r in db.execute('SELECT device,payload FROM outbox')}
    assert live[phone] == '{}' and live[legacy] == '{}'
    assert len(delivered) == 2  # Mac and the other authenticated owner remain eligible.
    assert set(live) == {phone, legacy, mac, other}


def test_resign_and_lease_expiry_resume_only_new_events(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('klaudimero_plugin.storage.time.time', lambda: clock[0])
    store = Store(tmp_path); enroll(store)
    client = str(uuid.uuid4())
    store.mac_activity('alice', client, 1, True)
    store.enqueue('japan', 'bot', 'while-mac-active', 'reply_ready')
    assert store.claim() is None
    # Duplicate heartbeats do not extend leases or resurrect suppressed events.
    clock[0] += 30
    store.mac_activity('alice', client, 1, True)
    clock[0] += 16
    store.enqueue('japan', 'bot', 'after-expiry', 'reply_ready')
    event = store.claim(); assert event is not None; store.settle(event['id'])
    store.enqueue('japan', 'bot', 'while-mac-active', 'reply_ready')
    assert store.claim() is None
    store.mac_activity('alice', client, 2, True)
    store.mac_activity('alice', client, 3, False)
    with pytest.raises(Problem, match='activity_sequence_stale'):
        store.mac_activity('alice', client, 2, True)
    store.enqueue('japan', 'bot', 'after-resign', 'reply_ready')
    assert store.claim() is not None


def test_pending_retries_are_retired_when_mac_becomes_active(tmp_path):
    store = Store(tmp_path); enroll(store)
    store.enqueue('japan', 'bot', 'retry', 'reply_ready')
    event = store.claim(); store.settle(event['id'], retry=True)
    store.mac_activity('alice', str(uuid.uuid4()), 1, True)
    with store.connection() as db:
        assert db.execute('SELECT payload FROM outbox').fetchone()[0] == '{}'
    # A late retry settlement cannot bring the discarded packet back.
    store.settle(event['id'], retry=True)
    assert store.claim() is None


def test_other_active_mac_keeps_phone_quiet_and_requires_no_apns_registration(tmp_path):
    store = Store(tmp_path); enroll(store)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    store.mac_activity('alice', a, 1, True)
    store.mac_activity('alice', b, 1, True)
    store.mac_activity('alice', a, 2, False)
    store.enqueue('japan', 'bot', 'two-macs', 'reply_ready')
    assert store.claim() is None
    store.mac_activity('alice', b, 2, False)
    store.enqueue('japan', 'bot', 'both-closed', 'reply_ready')
    assert store.claim() is not None


def test_activity_api_is_authenticated_strict_and_owner_scoped(client, monkeypatch):
    from klaudimero_plugin.delivery import Sender
    monkeypatch.setattr(Sender, 'start', lambda self: None)
    target = BASE + '/activity/' + str(uuid.uuid4())
    for body in [{'sequence': True, 'active': True}, {'sequence': 0, 'active': True}, {'sequence': 1, 'active': 'yes'}, {'sequence': 1, 'active': True, 'lease_seconds': 999999}]:
        assert client.put(target, json=body).status_code == 422
    assert client.put(target, json={'sequence': 1, 'active': True}).json() == {'sequence': 1, 'active': True, 'lease_seconds': 45}
    assert client.put(target, json={'sequence': 2, 'active': False}).status_code == 200
    assert client.put(target, json={'sequence': 1, 'active': True}).status_code == 409
    client.headers['Authorization'] = 'Bearer bob'
    assert client.put(target, json={'sequence': 1, 'active': True}).status_code == 200
    client.headers.pop('Authorization')
    assert client.put(target, json={'sequence': 3, 'active': True}).status_code == 401


def test_capabilities_advertise_preview_and_activity_in_same_release(client):
    capabilities = client.get(BASE + '/capabilities').json()
    assert capabilities['version'] == '0.6.0'
    assert capabilities['push_preview'] == 1
    assert capabilities['mac_activity'] == {'protocol': 1, 'lease_seconds': 45}
    assert capabilities['lifecycle']['protocol'] == 1


def test_legacy_device_storage_migrates_and_platform_api_retains_registration(client, tmp_path, monkeypatch):
    from klaudimero_plugin.delivery import Sender
    from klaudimero_plugin.api import services
    monkeypatch.setattr(Sender, 'start', lambda self: None)
    store = Store(tmp_path / 'legacy')
    device = enroll(store)
    with store.connection() as db: db.execute('ALTER TABLE devices DROP COLUMN platform')
    migrated = Store(store.directory)
    with migrated.connection() as db:
        row = db.execute('SELECT id,platform FROM devices').fetchone()
        assert row['id'] == device and row['platform'] == 'unknown'
    key = base64.b64encode(X25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    data = {'device_token':'a'*64, 'environment':'sandbox', 'sessions':[], 'encryption':{'version':2, 'public_key':key}, 'platform':'macos'}
    target = BASE+'/devices/'+str(uuid.uuid4())
    assert client.put(target,json=data).status_code == 200
    with services()[0].connection() as db:
        assert db.execute('SELECT platform FROM devices').fetchone()[0] == 'macos'
    assert client.put(target,json={**data,'platform':'android'}).status_code == 422
