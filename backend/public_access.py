"""Opt-in public gateway boundary. Disabled deployments retain private API behavior."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen


class PublicAccessError(Exception):
    def __init__(self, code: str, status: int, message: str):
        self.code, self.status, self.message = code, status, message
        self.retry_after = 60


@dataclass(frozen=True)
class PublicAccess:
    gateway_key: str = field(repr=False)
    turnstile_secret: str = field(repr=False)
    hostname: str
    database: str
    daily_limit: int = 300
    hourly_client_limit: int = 60
    result_ttl: int = 86400

    @classmethod
    def from_env(cls):
        mode = os.getenv('DEEPCHECK_PUBLIC_MODE', 'off')
        if mode == 'off':
            return None
        if mode != 'gateway':
            raise RuntimeError('DEEPCHECK_PUBLIC_MODE must be off or gateway')
        key = os.getenv('GATEWAY_SHARED_SECRET', '')
        secret = os.getenv('DEEPCHECK_TURNSTILE_SECRET', '')
        host = os.getenv('DEEPCHECK_TURNSTILE_HOSTNAME', '')
        database = os.getenv('DEEPCHECK_PUBLIC_STATE_FILE', '')
        if (len(key) < 32 or not key.isascii() or not key.isprintable()
                or not secret or not re.fullmatch(r'[a-z0-9.-]+', host)
                or not Path(database).is_absolute()):
            raise RuntimeError('Public gateway settings are incomplete')
        policy = cls(key, secret, host, database)
        # Fail startup if durable quotas cannot be opened. Never fall back to memory.
        with policy.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS quota (kind TEXT, subject TEXT, bucket INTEGER, n INTEGER, PRIMARY KEY(kind,subject,bucket))')
        return policy

    @contextmanager
    def connect(self):
        path = Path(self.database)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=3, isolation_level=None)
        try:
            os.chmod(path, 0o600)
            yield db
        finally:
            db.close()

    def authenticate(self, headers) -> str:
        supplied = headers.get('x-public-gateway-key', '')
        if not secrets.compare_digest(supplied.encode(), self.gateway_key.encode()):
            raise PublicAccessError('not_found', 404, '요청한 경로를 찾을 수 없습니다.')
        client = headers.get('x-public-client-id', '')
        if not re.fullmatch(r'[a-f0-9]{64}', client):
            raise PublicAccessError('invalid_gateway_request', 400, '요청 정보를 확인할 수 없습니다.')
        return client

    def charge(self, client: str, *, analysis: bool, now: int | None = None, challenge: bool = False):
        now = int(time.time()) if now is None else now
        limits = ([('daily', 'all', now // 86400, self.daily_limit),
                   ('hourly', client, now // 3600, self.hourly_client_limit)] if analysis
                  else [('poll_daily', 'all', now // 86400, 20000), ('poll', client, now // 60, 90)])
        if challenge:
            limits = [('challenge_daily', 'all', now // 86400, 1000), ('challenge', client, now // 60, 10)]
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # Prune old buckets (up to three days with daily bucket rounding).
                for kind, seconds in [('daily',86400),('poll_daily',86400),('hourly',3600),('poll',60),('challenge_daily',86400),('challenge',60)]:
                    db.execute('DELETE FROM quota WHERE kind=? AND bucket<?', (kind,(now-172800)//seconds))
                for kind, subject, bucket, maximum in limits:
                    row = db.execute('SELECT n FROM quota WHERE kind=? AND subject=? AND bucket=?', (kind,subject,bucket)).fetchone()
                    if row and row[0] >= maximum:
                        error = PublicAccessError('public_quota_exceeded', 429, '공개 체험 요청 한도에 도달했습니다. 안내된 시간 후 다시 시도해주세요.')
                        seconds = 86400 if kind in ('daily','poll_daily','challenge_daily') else 3600 if kind == 'hourly' else 60
                        error.retry_after = (bucket + 1) * seconds - now
                        raise error
                for kind,subject,bucket,_ in limits:
                    db.execute('INSERT INTO quota VALUES(?,?,?,1) ON CONFLICT(kind,subject,bucket) DO UPDATE SET n=n+1', (kind,subject,bucket))
                db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise PublicAccessError('public_unavailable', 503, '공개 접수를 잠시 사용할 수 없습니다.') from exc

    def verify_challenge(self, token: str):
        if not token or len(token) > 2048:
            raise PublicAccessError('challenge_required', 403, '요청 확인을 다시 진행해주세요.')
        request = Request('https://challenges.cloudflare.com/turnstile/v0/siteverify',
                          data=json.dumps({'secret':self.turnstile_secret,'response':token}).encode(),
                          headers={'Content-Type':'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read(8193)
                if len(raw)>8192:raise ValueError('oversize')
                result = json.loads(raw)
        except Exception as exc:
            raise PublicAccessError('challenge_unavailable', 503, '요청 확인 서비스를 잠시 사용할 수 없습니다.') from exc
        if (not isinstance(result,dict) or result.get('success') is not True
                or result.get('hostname') != self.hostname or result.get('action') != 'analyze'):
            raise PublicAccessError('challenge_failed', 403, '요청 확인을 다시 진행해주세요.')

    def issue_result_token(self, job_id: str, now: int | None = None) -> str:
        expires = (int(time.time()) if now is None else now) + self.result_ttl
        message=f'result-v1:{job_id}:{expires}'.encode()
        signature=hmac.new(self.gateway_key.encode(),message,hashlib.sha256).hexdigest()
        return f'{expires}.{signature}'

    def verify_result_token(self, job_id: str, authorization: str, now: int | None = None):
        match=re.fullmatch(r'Bearer ([0-9]{10})\.([a-f0-9]{64})',authorization)
        now=int(time.time()) if now is None else now
        if match:
            expires=int(match[1])
            expected=hmac.new(self.gateway_key.encode(),f'result-v1:{job_id}:{expires}'.encode(),hashlib.sha256).hexdigest()
            if now < expires <= now+self.result_ttl and secrets.compare_digest(expected,match[2]):return
        raise PublicAccessError('not_found',404,'해당 분석 결과를 확인할 수 없습니다.')
