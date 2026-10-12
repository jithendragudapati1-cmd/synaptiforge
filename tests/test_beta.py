"""Account self-service, global capacity and deletion recovery integration."""
import io,json,os,shutil,sys,tempfile,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='synaptiforge_beta_') as folder:
        base=Path(folder)
        for path in ROOT.glob('*.py'):shutil.copy2(path,base/path.name)
        for name in ('templates','static'):shutil.copytree(ROOT/name,base/name)
        os.chdir(base);sys.path.insert(0,str(base))
        for key in ('SYNAPTIFORGE_ENV','SYNAPTIFORGE_STORAGE_DIR','SMTP_USERNAME','SMTP_PASSWORD','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET','BACKUP_DIR','BACKUP_SECONDARY_DIR','BACKUP_ENCRYPTION_KEY'):os.environ.pop(key,None)
        os.environ.update(AUTH_DATA_DIR=str(base/'data/auth'),SECRET_KEY='beta-test-secret-with-more-than-32-characters',MAX_ACCOUNTS='50',GLOBAL_STORAGE_MB='1024',MAX_GLOBAL_QUEUED_JOBS='10')
        from app import app
        from auth_service import create_account,database,generate_password_hash,sign_in
        from job_queue import queue_db
        from backup_service import take_backup,restore_backup,successful_backups,backup_directory,mirror_backups
        app.config.update(TESTING=True,ACCOUNT_MAIL_INLINE_TEST=True)
        with app.app_context():
            first=create_account('First','first@example.com',generate_password_hash('safe-test-password-2026'),google_sub='first')
            second=create_account('Second','second@example.com',generate_password_hash('safe-test-password-2026'),google_sub='second')
            os.environ['MAX_ACCOUNTS']='2'
            try:create_account('Capped','capped@example.com')
            except ValueError:pass
            else:raise AssertionError('Account cap failed')
            os.environ['MAX_ACCOUNTS']='50'
        def login(email):
            client=app.test_client();client.get('/login')
            with client.session_transaction() as state:csrf=state['csrf_token']
            assert client.post('/login',data={'csrf_token':csrf,'email':email,'password':'safe-test-password-2026'}).status_code==302
            with client.session_transaction() as state:csrf=state['csrf_token']
            return client,{'X-CSRF-Token':csrf}
        one,h1=login('first@example.com');old,h_old=login('first@example.com');two,h2=login('second@example.com')
        sample=one.get('/api/sample/training').data
        def train(client,headers):return client.post('/api/train',data={'file':(io.BytesIO(sample),'test.csv'),'target':'outcome','workers':'1'},headers=headers)
        os.environ['MAX_GLOBAL_QUEUED_JOBS']='1'
        queued=train(one,h1);assert queued.status_code==202,queued.json
        assert train(two,h2).status_code==409
        job=queued.json['job_id']
        assert one.get('/api/account/export').status_code==409
        assert one.delete('/api/account',json={'confirmation':'first@example.com','password':'safe-test-password-2026'},headers=h1).status_code==409
        assert one.post('/api/jobs/'+job+'/cancel',headers=h1).status_code==200
        os.environ['GLOBAL_STORAGE_MB']='0';assert train(two,h2).status_code==409
        os.environ['GLOBAL_STORAGE_MB']='1024';os.environ['MIN_FREE_DISK_MB']='999999999';assert train(two,h2).status_code==409
        os.environ['MIN_FREE_DISK_MB']='256'
        response=one.get('/api/account/export');assert response.status_code==200
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            user=json.loads(archive.read('account.json'));assert user['email']=='first@example.com'
            assert not any('second' in name for name in archive.namelist())
            assert 'password_hash' not in user and 'google_sub' not in user
            assert any(name.endswith('.csv') for name in archive.namelist())
        assert one.delete('/api/account',json={'confirmation':'first@example.com','password':'wrong'},headers=h1).status_code==400
        assert one.delete('/api/account',json={'confirmation':'second@example.com','password':'safe-test-password-2026'},headers=h1).status_code==400
        assert take_backup();snapshot=successful_backups(backup_directory())[-1]
        assert one.delete('/api/account',json={'confirmation':'first@example.com','password':'safe-test-password-2026'},headers=h1).status_code==200
        assert old.get('/api/jobs').status_code==401 and one.get('/api/account/export').status_code==401
        assert not (base/'data/users'/str(first)).exists()
        assert two.get('/api/jobs').status_code==200
        with app.app_context():third=create_account('Replacement','replacement@example.com',google_sub='replacement');assert third>second
        os.environ['BACKUP_SECONDARY_DIR']=str(base.parent/(base.name+'-secondary'))
        assert mirror_backups(backup_directory())
        assert (Path(os.environ['BACKUP_SECONDARY_DIR'])/snapshot.name).read_bytes()==snapshot.read_bytes()
        assert (Path(os.environ['BACKUP_SECONDARY_DIR'])/'deletion-registry.sfd').exists()
        restored=restore_backup(snapshot,base/'restore')
        import sqlite3
        with sqlite3.connect(restored/'data/auth/accounts.sqlite3') as db:
            assert not db.execute('SELECT 1 FROM users WHERE id=?',(first,)).fetchone()
            assert db.execute('SELECT 1 FROM users WHERE id=?',(second,)).fetchone()
            assert db.execute('SELECT next_id FROM account_sequence').fetchone()[0]>first
        assert not (restored/'data/users'/str(first)).exists()
        # Google-only accounts require a recent Google sign-in.
        google=app.test_client()
        with google.session_transaction() as state:state.update(user_id=third,session_version=0,csrf_token='test-csrf',reauthenticated_at=0)
        assert google.delete('/api/account',json={'confirmation':'replacement@example.com'},headers={'X-CSRF-Token':'test-csrf'}).status_code==409
        import time
        with google.session_transaction() as state:state['reauthenticated_at']=int(time.time())
        assert google.delete('/api/account',json={'confirmation':'replacement@example.com'},headers={'X-CSRF-Token':'test-csrf'}).status_code==200
        with app.app_context():fourth=create_account('Fourth','fourth@example.com',google_sub='fourth');assert fourth>third
        shutil.rmtree(os.environ['BACKUP_SECONDARY_DIR'])
        print('PASS global account/queue/storage/disk limits; private data export; password/recent-Google deletion; active-job protection; old-session revocation; IDs never reused; deletion registry applied on restore')
        app.extensions['account_mail_executor'].shutdown(wait=True)

if __name__=='__main__':main()
