"""Private account exports and reauthenticated account deletion."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import zipfile
from flask import current_app,g,session
from werkzeug.security import check_password_hash
from workspace_storage import data_root,model_root,private_directory
from auth_service import database
from job_queue import queue_db,has_active

def lock_account(owner):
    root=Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or '.')
    directory=root/'data/account-locks';private_directory(directory)
    lock=(directory/(str(owner)+'.lock')).open('a');fcntl.flock(lock,fcntl.LOCK_EX)
    return lock

def export_account(owner):
    with queue_db() as queue:
        if has_active(owner,queue):raise RuntimeError('Wait for training to stop before exporting your workspace.')
    with database() as db:
        user=dict(db.execute('SELECT id,name,email,created,email_verified FROM users WHERE id=?',(owner,)).fetchone())
    files=[p for base in (data_root(owner),model_root(owner)) for p in base.rglob('*') if p.is_file()]
    if any(p.is_symlink() for p in files):raise ValueError('A workspace file needs operator review.')
    if sum(p.stat().st_size for p in files)>int(os.environ.get('MAX_EXPORT_MB','100'))*1024*1024:
        raise ValueError('Your export is too large. Contact support for a full export.')
    result=tempfile.TemporaryFile()
    try:
        with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('account.json',json.dumps(user,indent=2))
            from job_queue import read_job
            jobs=[read_job(owner,path.stem) for path in (data_root(owner)/'jobs').glob('*.json')]
            archive.writestr('jobs.json',json.dumps(jobs,indent=2))
            for base,prefix in ((data_root(owner),'workspace'),(model_root(owner),'models')):
                for path in files:
                    if path.is_relative_to(base):archive.write(path,prefix+'/'+str(path.relative_to(base)))
        result.seek(0);return result
    except Exception:result.close();raise

def delete_account(owner,confirmation,password):
    if confirmation!=g.user['email']:raise ValueError('Type your account email to confirm deletion.')
    from auth_service import limited
    if limited('account-delete:'+str(owner),5):raise RuntimeError('Too many deletion attempts. Please try again in 15 minutes.')
    with database() as db:user=db.execute('SELECT * FROM users WHERE id=?',(owner,)).fetchone()
    if user['password_hash']:
        if not isinstance(password,str) or len(password)>128 or not check_password_hash(user['password_hash'],password):
            raise ValueError('Enter your current password to delete your account.')
    elif time.time()-session.get('reauthenticated_at',0)>300:
        raise RuntimeError('Sign in with Google again, then delete your account within five minutes.')
    with queue_db() as queue:
        queue.execute('BEGIN IMMEDIATE')
        if has_active(owner,queue):raise RuntimeError('Cancel your queued or running training and wait for it to stop first.')
        queue.execute('INSERT OR IGNORE INTO deleted_accounts(owner,deleted_at) VALUES (?,?)',(owner,int(time.time())))
    # A committed tombstone revokes access even if later cleanup is interrupted.
    finish_deletion(owner)
    session.clear()


def finish_deletion(owner):
    with queue_db() as queue:
        queue.execute('BEGIN IMMEDIATE')
        if has_active(owner,queue):return False
        with database() as db:
            db.execute('DELETE FROM account_mail WHERE user_id=?',(owner,))
            user=db.execute('SELECT email FROM users WHERE id=?',(owner,)).fetchone()
            if user:
                from account_actions import cipher
                for row in db.execute("SELECT id,payload FROM account_mail WHERE payload!=''").fetchall():
                    try:email=json.loads(cipher().decrypt(row['payload'].encode()))['email']
                    except Exception:continue
                    if email==user['email']:db.execute('DELETE FROM account_mail WHERE id=?',(row['id'],))
        purge_account_files(Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or '.'),owner)
        queue.execute('DELETE FROM queue WHERE owner=?',(owner,));queue.execute('DELETE FROM labels WHERE owner=?',(owner,))
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM account_tokens WHERE user_id=?',(owner,))
            db.execute('DELETE FROM mail_outbox WHERE user_id=?',(owner,))
            db.execute('DELETE FROM users WHERE id=?',(owner,))
    return True


def retry_deletions():
    with queue_db() as queue:owners=queue.execute('SELECT owner FROM deleted_accounts').fetchall()
    for row in owners:
        owner=row['owner']
        with database() as db:exists=db.execute('SELECT 1 FROM users WHERE id=?',(owner,)).fetchone()
        if exists:
            lock=lock_account(owner)
            try:finish_deletion(owner)
            finally:fcntl.flock(lock,fcntl.LOCK_UN);lock.close()


def purge_account_files(root,owner):
    for path in (root/'data/users'/str(owner),root/'models/users'/str(owner)):
        if path.exists():shutil.rmtree(path)
    marker=root/'data/private_workspace_migration.json'
    if marker.exists() and json.loads(marker.read_text()).get('original_owner_id')==owner:
        # Pre-account historical copies belong only to the original migrated owner.
        from workspace_storage import valid_job_id
        for status in (root/'data/jobs').glob('*.json'):
            job=valid_job_id(status.stem)
            for path in (status,root/'data'/('training_'+job+'.csv'),root/'models/jobs'/(job+'.joblib'),root/'models/checkpoints'/('custom_'+job+'.joblib')):
                if path.is_file():path.unlink()
        for path in (root/'models/custom_distributed_model.joblib',root/'models/active_custom_model.json'):
            if path.is_file():path.unlink()
