import os
from dataclasses import asdict

import pytest

from backend.harness import Harness, Job, COMPLETED, CLOSED, AdmissionConflictError
from backend.result_store import ResultStore


def path(tmp_path):
    return str(tmp_path.resolve() / 'private' / 'results.json')


def test_atomic_roundtrip_and_exclusive_lock(tmp_path):
    p = path(tmp_path)
    store = ResultStore(p)
    assert store.read() == []
    with pytest.raises(BlockingIOError):
        ResultStore(p)
    store.write([{'value': '한글'}])
    store.close()
    restored = ResultStore(p)
    assert restored.read() == [{'value': '한글'}]
    assert os.stat(p).st_mode & 0o777 == 0o600
    restored.close()


def test_completed_job_survives_harness_restart(tmp_path):
    p = path(tmp_path)
    first = Harness(result_state_file=p)
    job = Job(id='a' * 32, session_id='session', url='https://youtu.be/example',
              params={}, status=COMPLETED, result={'answer': 'kept'})
    with first._lock:
        first._jobs[job.id] = job
        first._persist_results_locked()
    first.shutdown()
    second = Harness(result_state_file=p)
    try:
        assert asdict(second.get(job.id)) == asdict(job)
        assert second.stats()['result_persistence'] == 'durable-terminal-v1'
    finally:
        second.shutdown()


def test_disk_failure_closes_admission_and_durability(tmp_path, monkeypatch):
    h = Harness(result_state_file=path(tmp_path))
    try:
        def fail(_):
            raise OSError('disk full')
        monkeypatch.setattr(h._result_store, 'write', fail)
        with h._lock:
            h._persist_results_locked()
        assert h.stats()['admission_state'] == CLOSED
        assert h.stats()['result_persistence'] == 'unavailable'
        assert h.stats()['admission_protocol'] == 'unavailable'
        with pytest.raises(AdmissionConflictError):
            h.resume('11111111-1111-4111-8111-111111111111')
    finally:
        h.shutdown()


def test_corrupt_or_nonterminal_snapshot_fails_startup(tmp_path):
    p = path(tmp_path)
    store = ResultStore(p)
    store.write([asdict(Job(id='b' * 32, session_id='s', url='u', params={}))])
    store.close()
    with pytest.raises(ValueError, match='terminal'):
        Harness(result_state_file=p)


def test_rejects_symlink_parent(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir(mode=0o700)
    (tmp_path / 'alias').symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        ResultStore(str(tmp_path.resolve() / 'alias' / 'results.json'))


def test_failed_atomic_replace_preserves_previous_snapshot(tmp_path, monkeypatch):
    store = ResultStore(path(tmp_path))
    try:
        store.write([{'generation': 1}])
        def fail(*args, **kwargs):
            raise OSError('replace failed')
        monkeypatch.setattr(os, 'replace', fail)
        with pytest.raises(OSError):
            store.write([{'generation': 2}])
        assert store.read() == [{'generation': 1}]
    finally:
        store.close()


def test_restored_jobs_obey_existing_retention_limit(tmp_path):
    p = path(tmp_path)
    store = ResultStore(p)
    jobs = [Job(id=f'{i:032x}', session_id='s', url='u', params={},
                status=COMPLETED, updated_at=float(i)) for i in range(3)]
    store.write([asdict(j) for j in jobs])
    store.close()
    h = Harness(result_state_file=p, max_retained_jobs=2)
    try:
        assert h.get(jobs[0].id) is None
        assert len(h.all_jobs()) == 2
    finally:
        h.shutdown()
