"""Encrypted idle-time snapshots with SQLite online backups and bounded retention."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC=b'SYNAPTIFORGE-BACKUP-1\n'


def storage_root():
    return Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or Path(__file__).resolve().parent).resolve()


def backup_directory():
    root=storage_root()
    directory=Path(os.environ.get('BACKUP_DIR') or root/'backups/automatic').resolve()
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.chmod(0o700)
    if directory==root or any(directory.is_relative_to(root/name) for name in ('data','models')):
        raise ValueError('Use a separate backup directory.')
    return directory


def backup_key():
    secret=os.environ.get('BACKUP_ENCRYPTION_KEY') or os.environ.get('SECRET_KEY')
    if not secret:
        auth=Path(os.environ.get('AUTH_DATA_DIR') or storage_root()/'data/auth')
        secret=(auth/'session.key').read_text().strip()
    if len(secret)<32:raise ValueError('A stable encryption key of at least 32 characters is required.')
    return hashlib.sha256(('synaptiforge backups v1:'+secret).encode()).digest()


def artifact_manifest(root):
    result={}
    for directory in (root/'data',root/'models'):
        if not directory.exists():continue
        for path in directory.rglob('*'):
            if path.is_symlink():raise ValueError('Do not back up symlinked workspace files.')
            if not path.is_file():continue
            # Databases get SQLite's online backup API; transient files are never copied.
            if path.name.endswith(('.sqlite3','.sqlite3-wal','.sqlite3-shm','.tmp','.lock')):continue
            info=path.stat()
            result[str(path.relative_to(root))]=(info.st_size,info.st_mtime_ns,info.st_ino)
    return result


def database_paths(root):
    paths=list((root/'data').rglob('*.sqlite3')) if (root/'data').exists() else []
    auth=Path(os.environ.get('AUTH_DATA_DIR') or root/'data/auth').resolve()/'accounts.sqlite3'
    if auth.exists() and auth not in paths:paths.append(auth)
    return sorted(paths)


def active_training(root):
    path=root/'data/job_queue.sqlite3'
    if path.exists():
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=5) as db:
            if db.execute("SELECT 1 FROM queue WHERE state IN ('running','cancelling') LIMIT 1").fetchone():return True
    for path in (root/'data').glob('users/*/jobs/*.json'):
        try:
            if json.loads(path.read_text()).get('status') in ('starting','training','cancelling'):return True
        except (OSError,ValueError):return True
    return False


def record_status(directory,status,**details):
    value={'status':status,'checked_at':datetime.now(timezone.utc).isoformat(),**details}
    previous=directory/'status.json'
    if previous.exists():
        try:old=json.loads(previous.read_text())
        except (OSError,ValueError):old={}
        if 'last_success' in old and 'last_success' not in value:value['last_success']=old['last_success']
    temporary=directory/'status.tmp'
    temporary.write_text(json.dumps(value));temporary.chmod(0o600);os.replace(temporary,previous)


def successful_backups(directory):
    return sorted(directory.glob('snapshot-*.sfb'),key=lambda path:path.stat().st_mtime)


def prune(directory):
    keep=max(1,int(os.environ.get('BACKUP_KEEP','7')))
    age=max(1,int(os.environ.get('BACKUP_RETENTION_DAYS','7')))*86400
    paths=successful_backups(directory)
    for path in list(paths):
        if path.stat().st_mtime<time.time()-age or len(paths)>keep:
            path.unlink();paths.remove(path)


def encrypt_archive(source,destination,key):
    nonce=os.urandom(12)
    cipher=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(MAGIC)
    with source.open('rb') as original,destination.open('xb') as encrypted:
        destination.chmod(0o600)
        encrypted.write(MAGIC+nonce)
        while chunk:=original.read(1024*1024):encrypted.write(cipher.update(chunk))
        encrypted.write(cipher.finalize());encrypted.write(cipher.tag)
        encrypted.flush();os.fsync(encrypted.fileno())


def take_backup():
    root=storage_root();directory=backup_directory()
    with (directory/'worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return False
        if active_training(root):
            record_status(directory,'waiting',reason='Training is active; snapshot deferred.');return False
        key=backup_key();manifest=artifact_manifest(root);databases=database_paths(root)
        bytes_needed=sum(value[0] for value in manifest.values())+sum(path.stat().st_size for path in databases)
        if bytes_needed>int(os.environ.get('BACKUP_MAX_MB','1024'))*1024*1024:
            raise ValueError('Workspace exceeds the configured backup size limit.')
        prune(directory)
        reserve=bytes_needed*3+32*1024*1024
        paths=successful_backups(directory)
        while shutil.disk_usage(directory).free<reserve and len(paths)>1:
            paths.pop(0).unlink()
        if shutil.disk_usage(directory).free<reserve:raise ValueError('Not enough free storage for a safe backup.')
        timestamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        final=directory/('snapshot-'+timestamp+'.sfb')
        with tempfile.TemporaryDirectory(prefix='.snapshot-',dir=directory) as temporary:
            temp=Path(temporary);temp.chmod(0o700);archive=temp/'snapshot.zip'
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
                for relative in manifest:bundle.write(root/relative,relative)
                for index,path in enumerate(databases):
                    snapshot=temp/('database-'+str(index)+'.sqlite3')
                    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=10) as original,sqlite3.connect(snapshot) as output:
                        original.backup(output)
                        if output.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Database integrity check failed.')
                    snapshot.chmod(0o600)
                    try:relative=str(path.relative_to(root))
                    except ValueError:relative='data/auth/accounts.sqlite3'
                    bundle.write(snapshot,relative)
                bundle.writestr('backup-manifest.json',json.dumps({'version':1,'created_at':timestamp,'files':sorted(manifest),'databases':len(databases)}))
            if active_training(root) or artifact_manifest(root)!=manifest:
                record_status(directory,'waiting',reason='Workspace changed; snapshot deferred.');return False
            encrypted=temp/'encrypted.sfb';encrypt_archive(archive,encrypted,key)
            os.replace(encrypted,final)
        record_status(directory,'ok',last_success=datetime.now(timezone.utc).isoformat(),file=final.name)
        prune(directory)
        mirrored=mirror_backups(directory)
        record_status(directory,'ok',secondary_copy=mirrored)
        return True


def restore_backup(archive,destination):
    """Verify encryption before extracting; never overwrite a live workspace."""
    archive=Path(archive).resolve();destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):raise ValueError('Restore only into an empty directory.')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700);destination.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix='synaptiforge-restore-') as temporary:
        decrypted=Path(temporary)/'snapshot.zip'
        with archive.open('rb') as encrypted,decrypted.open('xb') as original:
            decrypted.chmod(0o600)
            if encrypted.read(len(MAGIC))!=MAGIC:raise ValueError('Unsupported backup format.')
            nonce=encrypted.read(12);remaining=archive.stat().st_size-len(MAGIC)-12-16
            if remaining<0:raise ValueError('Incomplete backup.')
            encrypted.seek(-16,2);tag=encrypted.read(16);encrypted.seek(len(MAGIC)+12)
            cipher=Cipher(algorithms.AES(backup_key()),modes.GCM(nonce,tag)).decryptor()
            cipher.authenticate_additional_data(MAGIC)
            while remaining:
                chunk=encrypted.read(min(1024*1024,remaining))
                if not chunk:raise ValueError('Incomplete backup.')
                original.write(cipher.update(chunk));remaining-=len(chunk)
            original.write(cipher.finalize()) # Reject wrong keys or any tampering before extraction.
        with zipfile.ZipFile(decrypted) as bundle:
            for member in bundle.infolist():
                path=Path(member.filename)
                if path.is_absolute() or '..' in path.parts or (member.external_attr>>16)&0o170000==0o120000:
                    raise ValueError('Unsafe backup entry.')
            for member in bundle.infolist():
                target=destination/member.filename
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with bundle.open(member) as source,target.open('xb') as output:shutil.copyfileobj(source,output)
                target.chmod(0o600)
    apply_deletion_registry(destination)
    return destination


def _mirror_backups(directory):
    """Copy only authenticated encrypted archives to a configured synced folder."""
    configured=os.environ.get('BACKUP_SECONDARY_DIR','').strip()
    if not configured:return False
    secondary=Path(configured).expanduser().resolve()
    if secondary.is_relative_to(storage_root()):raise ValueError('Use a backup destination outside the app storage.')
    secondary.mkdir(parents=True,exist_ok=True,mode=0o700)
    primary={path.name:path for path in successful_backups(directory)}
    state_path=directory/'secondary-files.json'
    try:state=json.loads(state_path.read_text())
    except (OSError,ValueError):state={}
    updated={}
    for name,path in primary.items():
        target=secondary/name
        def digest(file):
            value=hashlib.sha256()
            with file.open('rb') as stream:
                while chunk:=stream.read(1024*1024):value.update(chunk)
            return value.digest()
        info=path.stat()
        stamp=[info.st_size,info.st_mtime_ns]
        if target.exists():
            remote=target.stat();fingerprint=stamp+[remote.st_size,remote.st_mtime_ns]
            if state.get(name)==fingerprint or digest(path)==digest(target):
                updated[name]=fingerprint;continue
        temporary=secondary/(name+'.tmp')
        try:
            shutil.copyfile(path,temporary);temporary.chmod(0o600)
            if digest(path)!=digest(temporary):raise ValueError('Backup mirror verification failed.')
            os.replace(temporary,target)
            remote=target.stat();updated[name]=stamp+[remote.st_size,remote.st_mtime_ns]
        finally:
            if temporary.exists():temporary.unlink()
    # Retention only affects this app's own snapshot format in its dedicated folder.
    for path in secondary.glob('snapshot-*.sfb'):
        if path.name not in primary:path.unlink()
    state_path.write_text(json.dumps(updated));state_path.chmod(0o600)
    mirror_deletion_registry(directory,secondary)
    return True


def apply_deletion_registry(destination):
    current=storage_root()/'data/job_queue.sqlite3'
    deleted=set()
    secondary=os.environ.get('BACKUP_SECONDARY_DIR','')
    if secondary and (Path(secondary)/'deletion-registry.sfd').exists():
        from cryptography.fernet import Fernet
        import base64
        raw=Fernet(base64.urlsafe_b64encode(backup_key())).decrypt((Path(secondary)/'deletion-registry.sfd').read_bytes())
        deleted.update(json.loads(raw)['owners'])
    if current.exists():
        with sqlite3.connect('file:'+str(current)+'?mode=ro',uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='deleted_accounts'").fetchone():
                deleted.update(row[0] for row in db.execute('SELECT owner FROM deleted_accounts'))
    auth=destination/'data/auth/accounts.sqlite3'
    if auth.exists():
        with sqlite3.connect(auth) as db:
            for owner in deleted:
                mail_columns={row[1] for row in db.execute('PRAGMA table_info(account_mail)')}
                if 'user_id' in mail_columns:db.execute('DELETE FROM account_mail WHERE user_id=?',(owner,))
                for table in ('account_tokens','mail_outbox'):
                    if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():db.execute('DELETE FROM '+table+' WHERE user_id=?',(owner,))
                db.execute('DELETE FROM users WHERE id=?',(owner,))
            columns={row[1] for row in db.execute('PRAGMA table_info(users)')}
            if 'session_version' in columns:db.execute('UPDATE users SET session_version=abs(random())')
            db.execute('CREATE TABLE IF NOT EXISTS account_sequence(singleton INTEGER PRIMARY KEY,next_id INTEGER NOT NULL)')
            floor=max([0,*deleted,db.execute('SELECT coalesce(max(id),0) FROM users').fetchone()[0]])+1
            db.execute('INSERT OR IGNORE INTO account_sequence VALUES(1,?)',(floor,))
            db.execute('UPDATE account_sequence SET next_id=max(next_id,?)',(floor,))
    queue=destination/'data/job_queue.sqlite3'
    if queue.exists():
        with sqlite3.connect(queue) as db:
            db.execute('CREATE TABLE IF NOT EXISTS deleted_accounts(owner INTEGER PRIMARY KEY,deleted_at INTEGER NOT NULL)')
            for owner in deleted:
                db.execute('INSERT OR IGNORE INTO deleted_accounts VALUES(?,?)',(owner,int(time.time())))
                for table in ('queue','labels'):
                    columns={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
                    if 'owner' in columns:db.execute('DELETE FROM '+table+' WHERE owner=?',(owner,))
    from account_management import purge_account_files
    for owner in deleted:purge_account_files(destination,owner)


def mirror_deletion_registry(directory,secondary):
    import base64
    from cryptography.fernet import Fernet
    current=storage_root()/'data/job_queue.sqlite3'
    owners=[]
    if current.exists():
        with sqlite3.connect('file:'+str(current)+'?mode=ro',uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='deleted_accounts'").fetchone():owners=[row[0] for row in db.execute('SELECT owner FROM deleted_accounts ORDER BY owner')]
    raw=json.dumps({'owners':owners}).encode();digest=hashlib.sha256(raw).hexdigest()
    marker=directory/'secondary-registry.sha256';target=secondary/'deletion-registry.sfd'
    if target.exists() and marker.exists() and marker.read_text()==digest:
        try:
            if Fernet(base64.urlsafe_b64encode(backup_key())).decrypt(target.read_bytes())==raw:return
        except Exception:pass
    temporary=secondary/'deletion-registry.tmp'
    temporary.write_bytes(Fernet(base64.urlsafe_b64encode(backup_key())).encrypt(raw));temporary.chmod(0o600)
    os.replace(temporary,target);marker.write_text(digest);marker.chmod(0o600)


def mirror_backups(directory):
    with (directory/'secondary.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _mirror_backups(directory)
