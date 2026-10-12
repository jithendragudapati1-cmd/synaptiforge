"""Verify encrypted snapshot integrity, isolation, retention and safe restoration."""
from pathlib import Path
import json,os,sqlite3,sys,tempfile,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backup_service import take_backup,restore_backup,successful_backups,backup_directory,prune
from cryptography.exceptions import InvalidTag

def main():
    with tempfile.TemporaryDirectory(prefix='synaptiforge_backups_') as folder:
        root=Path(folder)/'workspace';root.mkdir()
        os.environ.update(SYNAPTIFORGE_STORAGE_DIR=str(root),SECRET_KEY='backup-test-signing-key-with-at-least-32-characters',BACKUP_KEEP='2',BACKUP_RETENTION_DAYS='7')
        for key in ('AUTH_DATA_DIR','BACKUP_DIR','BACKUP_ENCRYPTION_KEY','BACKUP_SECONDARY_DIR'):os.environ.pop(key,None)
        auth=root/'data/auth';auth.mkdir(parents=True)
        with sqlite3.connect(auth/'accounts.sqlite3') as db:
            db.execute('CREATE TABLE users(id INTEGER,name TEXT)');db.execute("INSERT INTO users VALUES (1,'Test user')")
        with sqlite3.connect(root/'data/job_queue.sqlite3') as db:
            db.execute('CREATE TABLE queue(state TEXT)')
        upload=root/'data/users/1/uploads/example.csv';upload.parent.mkdir(parents=True)
        upload.write_text('feature_1,outcome\n0,0\n1,1\n')
        model=root/'models/users/1/jobs/example.joblib';model.parent.mkdir(parents=True);model.write_bytes(b'test-model-content')
        assert take_backup()
        snapshots=successful_backups(backup_directory());assert len(snapshots)==1
        assert b'Test user' not in snapshots[0].read_bytes() and b'feature_1' not in snapshots[0].read_bytes()
        restored=restore_backup(snapshots[0],Path(folder)/'restored')
        assert (restored/upload.relative_to(root)).read_bytes()==upload.read_bytes()
        assert (restored/model.relative_to(root)).read_bytes()==model.read_bytes()
        with sqlite3.connect(restored/'data/auth/accounts.sqlite3') as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert db.execute('SELECT name FROM users').fetchone()[0]=='Test user'
        try:restore_backup(snapshots[0],restored)
        except ValueError:pass
        else:raise AssertionError('Overwrote an existing workspace')
        original_key=os.environ['SECRET_KEY'];os.environ['SECRET_KEY']='wrong-backup-key-with-at-least-32-characters'
        try:restore_backup(snapshots[0],Path(folder)/'wrong-key')
        except InvalidTag:pass
        else:raise AssertionError('Wrong encryption key accepted')
        os.environ['SECRET_KEY']=original_key
        damaged=Path(folder)/'tampered.sfb';content=bytearray(snapshots[0].read_bytes());content[50]^=1;damaged.write_bytes(content)
        try:restore_backup(damaged,Path(folder)/'tampered-restore')
        except InvalidTag:pass
        else:raise AssertionError('Tampered archive accepted')
        assert not any((Path(folder)/'tampered-restore').iterdir())
        with sqlite3.connect(root/'data/job_queue.sqlite3') as db:db.execute("INSERT INTO queue VALUES ('running')")
        assert not take_backup()
        with sqlite3.connect(root/'data/job_queue.sqlite3') as db:db.execute('DELETE FROM queue')
        assert take_backup();assert take_backup();assert len(successful_backups(backup_directory()))==2
        oldest=successful_backups(backup_directory())[0];os.utime(oldest,(time.time()-8*86400,time.time()-8*86400));prune(backup_directory())
        assert not oldest.exists()
        (backup_directory()/'status.json').write_text('damaged-json')
        assert take_backup()
        assert json.loads((backup_directory()/'status.json').read_text())['status']=='ok'
        print('PASS encrypted backup, SQLite integrity, full artifact restore, wrong-key/tamper rejection, no overwrite, active-job deferral and retention')

if __name__=='__main__':main()
