"""Durable single-machine queue with account-scoped job controls."""
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
import json
import os
import sqlite3
import time

from workspace_storage import (data_root, model_root, jobs_directory, job_status_path,
                               upload_path, checkpoint_path, private_directory, valid_job_id, write_json)

ACTIVE = ('queued','starting','training','cancelling')


def queue_path():
    root = Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or '.')
    return root / 'data/job_queue.sqlite3'


@contextmanager
def queue_db():
    private_directory(queue_path().parent)
    db=sqlite3.connect(queue_path(),timeout=15)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, job TEXT NOT NULL,
            state TEXT NOT NULL, resume INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL, pid INTEGER, cancel INTEGER NOT NULL DEFAULT 0,
            UNIQUE(owner,job));
        CREATE TABLE IF NOT EXISTS labels(owner INTEGER NOT NULL,job TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(owner,job));
        CREATE TABLE IF NOT EXISTS service_state(name TEXT PRIMARY KEY,updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS deleted_accounts(owner INTEGER PRIMARY KEY,deleted_at INTEGER NOT NULL);
        ''')
        yield db
        db.commit()
    except Exception:
        db.rollback();raise
    finally:
        db.close()


def storage_used(owner):
    return sum(path.stat().st_size for base in (data_root(owner),model_root(owner))
               for path in base.rglob('*') if path.is_file())


def read_job(owner,job):
    path=job_status_path(valid_job_id(job),owner)
    if not path.is_file():raise FileNotFoundError('Job not found.')
    status=json.loads(path.read_text())
    if status.get('owner_id') not in (None,owner):raise FileNotFoundError('Job not found.')
    with queue_db() as db:
        row=db.execute('SELECT * FROM queue WHERE owner=? AND job=?',(owner,job)).fetchone()
        label=db.execute('SELECT name FROM labels WHERE owner=? AND job=?',(owner,job)).fetchone()
    if row and row['state'] in ('queued','cancelling','cancelled','deleted'):
        status['status']=row['state']
        status['message']={'queued':'Waiting for the training worker.','cancelling':'Stopping training safely.','cancelled':'Training cancelled.','deleted':'Job deleted.'}[row['state']]
    status['name']=label['name'] if label else status.get('name') or 'Training job '+job
    status.setdefault('created_at',datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat())
    return status


def has_active(owner,db):
    queued=db.execute("SELECT 1 FROM queue WHERE owner=? AND state IN ('queued','running','cancelling')",(owner,)).fetchone()
    if queued:return True
    # Honor jobs launched by an older app version while they finish.
    for path in jobs_directory(owner).glob('*.json'):
        try:
            job=json.loads(path.read_text())
            if job.get('status') not in ('starting','training'):continue
            pid=job.get('pid')
            if not pid:continue
            os.kill(int(pid),0)
            return True
        except (OSError,ValueError,TypeError):continue
    return False


def enqueue(owner,job,status,content=None,resume=False,limits=None):
    limits=limits or {}
    with queue_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if has_active(owner,db):raise RuntimeError('You already have a queued or running job. Wait or cancel it first.')
        from capacity import check_capacity
        check_capacity(db,len(content) if content else 0)
        paths=list(jobs_directory(owner).glob('*.json'))
        if not resume and len(paths)>=limits.get('max_jobs',100):raise ValueError('Your saved-job limit is reached. Delete an old job first.')
        if storage_used(owner)+(len(content) if content else 0)+200000>limits.get('storage_bytes',100*1024*1024):
            raise ValueError('Your storage limit is reached. Delete unwanted jobs first.')
        if not resume and job_status_path(job,owner).exists():raise RuntimeError('Job ID conflict. Please try again.')
        if content is not None:
            path=upload_path(job,owner);private_directory(path.parent);path.write_bytes(content)
        status.update(owner_id=owner,job_id=job,status='queued',message='Waiting for the training worker.')
        status.setdefault('created_at',datetime.now(timezone.utc).isoformat())
        write_json(job_status_path(job,owner),status)
        db.execute("INSERT INTO queue(owner,job,state,resume,created,cancel) VALUES (?,?,'queued',?,?,0) ON CONFLICT(owner,job) DO UPDATE SET state='queued',resume=excluded.resume,created=excluded.created,pid=NULL,cancel=0",(owner,job,int(resume),int(time.time())))
        if status.get('name'):db.execute('INSERT OR REPLACE INTO labels(owner,job,name) VALUES (?,?,?)',(owner,job,status['name']))
    return status


def rename_job(owner,job,name):
    read_job(owner,job)
    name=str(name).strip()
    if not 1<=len(name)<=80:raise ValueError('Use a name between 1 and 80 characters.')
    with queue_db() as db:db.execute('INSERT OR REPLACE INTO labels(owner,job,name) VALUES (?,?,?)',(owner,job,name))


def cancel_job(owner,job):
    status=read_job(owner,job)
    with queue_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state FROM queue WHERE owner=? AND job=?',(owner,job)).fetchone()
        if not row or row['state'] not in ('queued','running','cancelling'):
            raise RuntimeError('This job is not running in the queue.')
        state='cancelled' if row['state']=='queued' else 'cancelling'
        db.execute('UPDATE queue SET state=?,cancel=1 WHERE owner=? AND job=?',(state,owner,job))
        if state=='cancelled':
            status.update(status=state,message='Training cancelled before it started.')
            write_json(job_status_path(job,owner),status)
    return state


def delete_job(owner,job):
    status=read_job(owner,job)
    with queue_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state FROM queue WHERE owner=? AND job=?',(owner,job)).fetchone()
        if status['status'] in ACTIVE or (row and row['state'] in ('queued','running','cancelling')):
            raise RuntimeError('Cancel the job and wait for it to stop before deleting it.')
        from custom_model_service import saved_model_path,active_job_id
        selected=active_job_id(owner)
        for path in (saved_model_path(job,owner),checkpoint_path(job,owner),upload_path(job,owner)):
            if path.is_file():path.unlink()
        if selected==job:
            pointer=model_root(owner)/'active_custom_model.json'
            if pointer.exists():pointer.unlink()
            # Do not silently activate an older model after deleting the selection.
        job_status_path(job,owner).unlink()
        db.execute('DELETE FROM queue WHERE owner=? AND job=?',(owner,job))
        db.execute('DELETE FROM labels WHERE owner=? AND job=?',(owner,job))


def heartbeat(name):
    with queue_db() as db:db.execute('INSERT OR REPLACE INTO service_state(name,updated) VALUES (?,?)',(name,int(time.time())))


def worker_ready():
    with queue_db() as db:row=db.execute("SELECT updated FROM service_state WHERE name='training' ").fetchone()
    return bool(row and time.time()-row['updated']<30)
