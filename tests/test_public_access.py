"""Public boundary tests use fake Siteverify and never run an analysis."""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend import app as api
from backend import public_access as module
from backend.public_access import PublicAccess, PublicAccessError


@pytest.fixture
def policy(monkeypatch, tmp_path):
    monkeypatch.setenv('DEEPCHECK_PUBLIC_MODE', 'gateway')
    monkeypatch.setenv('DEEPCHECK_PUBLIC_GATEWAY_KEY', 'a' * 64)
    monkeypatch.setenv('DEEPCHECK_TURNSTILE_SECRET', 'test-secret')
    monkeypatch.setenv('DEEPCHECK_TURNSTILE_HOSTNAME', 'chamsae-ai.vercel.app')
    monkeypatch.setenv('DEEPCHECK_PUBLIC_STATE_FILE', str(tmp_path / 'quota.sqlite3'))
    return PublicAccess.from_env()


def test_missing_config_fails_closed(policy, monkeypatch):
    monkeypatch.delenv('DEEPCHECK_PUBLIC_GATEWAY_KEY')
    with pytest.raises(RuntimeError):
        PublicAccess.from_env()
    monkeypatch.setenv('DEEPCHECK_PUBLIC_MODE', 'off')
    assert PublicAccess.from_env() is None


def test_quota_survives_restart_and_is_atomic(policy):
    def submit(_):
        try:
            PublicAccess.from_env().charge('client', analysis=True, now=1800000000)
            return True
        except PublicAccessError as exc:
            assert exc.status == 429
            assert 0 < exc.retry_after <= 3600
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(submit, range(12))) == 3
    policy.charge('client', analysis=True, now=1800003600)


def test_global_budget_and_failed_charge_are_atomic(policy):
    policy = replace(policy, daily_limit=1)
    policy.charge('one', analysis=True, now=1800000000)
    with pytest.raises(PublicAccessError) as raised:
        policy.charge('two', analysis=True, now=1800000000)
    assert raised.value.status == 429
    with policy.connect() as db:
        assert db.execute("SELECT n FROM quota WHERE subject='two'").fetchone() is None


def test_unavailable_quota_store_rejects_request(policy):
    with policy.connect() as db:
        db.execute('DROP TABLE quota')
    with pytest.raises(PublicAccessError) as raised:
        policy.charge('client', analysis=True)
    assert raised.value.status == 503


@pytest.mark.parametrize('change', [
    {'success': False}, {'hostname': 'attacker.example'}, {'action': 'login'},
])
def test_challenge_checks_all_fields(policy, monkeypatch, change):
    reply = {'success': True, 'hostname': policy.hostname, 'action': 'analyze'} | change
    monkeypatch.setattr(module, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps(reply).encode()))
    with pytest.raises(PublicAccessError) as raised:
        policy.verify_challenge('fake-token')
    assert raised.value.status == 403


def test_siteverify_failure_and_success(policy, monkeypatch):
    monkeypatch.setattr(module, 'urlopen', lambda *a, **k: io.BytesIO(b'not-json'))
    with pytest.raises(PublicAccessError) as raised:
        policy.verify_challenge('fake-token')
    assert raised.value.status == 503
    def verify(request, timeout):
        assert request.full_url == 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
        assert json.loads(request.data) == {'secret': 'test-secret', 'response': 'fake-token'}
        assert timeout == 5
        return io.BytesIO(json.dumps({'success': True, 'hostname': policy.hostname, 'action': 'analyze'}).encode())
    monkeypatch.setattr(module, 'urlopen', verify)
    policy.verify_challenge('fake-token')


def test_capability_is_job_bound_expiring_and_rotation_revokes(policy):
    now = 1800000000
    token = policy.issue_result_token('a' * 32, now)
    policy.verify_result_token('a' * 32, 'Bearer ' + token, now)
    for job, auth, instant, verifier in [
        ('b' * 32, 'Bearer ' + token, now, policy),
        ('a' * 32, 'Bearer ' + token, now + policy.result_ttl, policy),
        ('a' * 32, token, now, policy),
        ('a' * 32, 'Bearer ' + token, now, replace(policy, gateway_key='b' * 64)),
    ]:
        with pytest.raises(PublicAccessError) as raised:
            verifier.verify_result_token(job, auth, instant)
        assert raised.value.status == 404


def test_api_rejects_before_harness_and_does_not_store_challenge(policy, monkeypatch):
    class NoHarness:
        def get(self, *args):
            pytest.fail('unauthorized request reached harness')
        def submit(self, *args, **kwargs):
            pytest.fail('unauthorized request reached harness')
    monkeypatch.setattr(api, 'public_access', policy)
    monkeypatch.setattr(api, 'harness', NoHarness())
    # No lifespan: deliberately install the boundary and fake harness above.
    client = TestClient(api.app)
    headers = {'x-public-gateway-key': policy.gateway_key, 'x-public-client-id': 'c' * 64}
    url = 'https://www.youtube.com/watch?v=cYRkZmBuDqI'
    assert client.post('/api/analyze', json={'url': url}).status_code == 404
    assert client.post('/api/analyze', headers=headers, json={'url': url}).status_code == 403
    assert client.post('/api/analyze', headers=headers, json={'url': url, 'max_frames': 64}).status_code == 422
    assert client.get('/api/jobs/' + 'a' * 32, headers=headers).status_code == 404
    assert client.get('/api/jobs', headers=headers).status_code == 404
    assert client.get('/api/sessions/anything', headers=headers).status_code == 404
    with policy.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM quota').fetchone()[0] == 0
    assert 'turnstile_token' not in api.AnalyzeRequest(url=url, turnstile_token='private-token').model_dump()


def test_public_submit_and_result_flow_with_fake_harness(policy, monkeypatch):
    from types import SimpleNamespace
    job_id = 'd' * 32
    job = SimpleNamespace(id=job_id, status='queued', to_dict=lambda: {
        'id': job_id, 'status': 'queued', 'session_id': 'private-group',
    })
    def submit(**kwargs):
        assert 'turnstile_token' not in kwargs['params']
        return job, False
    monkeypatch.setattr(api, 'public_access', policy)
    monkeypatch.setattr(api, 'harness', SimpleNamespace(submit=submit, get=lambda value: job if value == job_id else None))
    tokens = set()
    def verify(self, token):
        if token in tokens:
            raise PublicAccessError('challenge_failed', 403, 'already used')
        tokens.add(token)
    monkeypatch.setattr(PublicAccess, 'verify_challenge', verify)
    client = TestClient(api.app)
    headers = {'x-public-gateway-key': policy.gateway_key, 'x-public-client-id': 'c' * 64}
    body = {'url': 'https://youtu.be/cYRkZmBuDqI', 'turnstile_token': 'one-use'}
    submitted = client.post('/api/analyze', headers=headers, json=body)
    assert submitted.status_code == 200
    token = submitted.json()['job_access_token']
    result = client.get('/api/jobs/' + job_id, headers=headers | {'Authorization': 'Bearer ' + token})
    assert result.status_code == 200
    assert 'session_id' not in result.json()
    assert client.post('/api/analyze', headers=headers, json=body).status_code == 403
    with policy.connect() as db:
        assert db.execute("SELECT n FROM quota WHERE kind='daily'").fetchone()[0] == 1
