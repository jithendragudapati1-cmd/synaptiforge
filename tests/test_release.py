"""Isolated release integration check; sends no real emails or changes real users."""
import io
import json
import os
from pathlib import Path
import py_compile
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='synaptiforge_release_') as directory:
        base=Path(directory)
        for file in ROOT.glob('*.py'):
            shutil.copy2(file,base/file.name);py_compile.compile(str(base/file.name),doraise=True)
        for name in ('templates','static'):shutil.copytree(ROOT/name,base/name)
        os.chdir(base);sys.path.insert(0,str(base))
        os.environ.pop('BACKUP_SECONDARY_DIR',None)
        for key in ('SMTP_USERNAME','SMTP_PASSWORD','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET','SYNAPTIFORGE_STORAGE_DIR','SYNAPTIFORGE_ENV'):
            os.environ.pop(key,None)
        os.environ['AUTH_DATA_DIR']=str(base/'data/auth')
        from app import app
        from auth_service import create_account,database,generate_password_hash
        from job_queue import queue_db
        from workspace_storage import checkpoint_path,upload_path,job_status_path
        from custom_model_service import saved_model_path
        app.config.update(TESTING=True,ACCOUNT_MAIL_INLINE_TEST=True)
        users=[]
        for index in range(3):
            with app.app_context():uid=create_account('Test User '+str(index),'user'+str(index)+'@example.com',generate_password_hash('release-test-password-2026'),google_sub='test-sub-'+str(index))
            client=app.test_client();client.get('/login')
            with client.session_transaction() as state:token=state['csrf_token']
            assert client.post('/login',data={'csrf_token':token,'email':'user'+str(index)+'@example.com','password':'release-test-password-2026'}).status_code==302
            with client.session_transaction() as state:token=state['csrf_token']
            users.append((uid,client,{'X-CSRF-Token':token}))
        owner1,one,h1=users[0];owner2,two,h2=users[1];owner3,three,h3=users[2]
        assert app.test_client().get('/healthz').status_code==200
        assert app.test_client().get('/api/jobs').status_code==401
        assert one.get('/api/jobs').json['jobs']==[]
        assert one.get('/api/model').status_code==404 # Original demo API removed.
        html=one.get('/').get_data(as_text=True)
        assert 'custom-feature-inputs' not in html and 'Original Demonstration Model' not in html
        assert all('data-tab="'+name+'"' in html for name in ('train','models','predict','account'))
        sample=one.get('/api/sample/training').data
        assert sample.startswith(b'feature_1,feature_2,outcome')
        def train(client,headers,**values):
            data={'file':(io.BytesIO(sample),'sample.csv'),'target':'outcome','workers':'1','name':'My model'};data.update(values)
            return client.post('/api/train',data=data,headers=headers)
        first=train(one,h1);assert first.status_code==202,first.json
        cancelled=first.json['job_id']
        assert train(one,h1).status_code==409
        assert one.delete('/api/jobs/'+cancelled,json={'confirmation':cancelled},headers=h1).status_code==409
        assert one.post('/api/jobs/'+cancelled+'/cancel',headers=h1).json['status']=='cancelled'
        assert one.delete('/api/jobs/'+cancelled,json={'confirmation':cancelled},headers=h1).status_code==200
        second=train(two,h2,workers='4',demo_mode='true',simulate_failure='true',checkpoint_demo='true');assert second.status_code==202,second.json
        second_id=second.json['job_id']
        first=train(one,h1,simulate_failure='true');assert first.status_code==202,first.json
        first_id=first.json['job_id']
        assert one.patch('/api/jobs/'+first_id,json={'name':'Renamed model'},headers=h1).status_code==200
        assert two.get('/api/jobs/'+first_id).status_code==404
        assert two.patch('/api/jobs/'+first_id,json={'name':'Not yours'},headers=h2).status_code==404
        assert two.delete('/api/jobs/'+first_id,json={'confirmation':first_id},headers=h2).status_code==404
        worker_log=(base/'worker.log').open('w')
        worker=subprocess.Popen([sys.executable,str(base/'task_worker.py')],cwd=str(base),stdout=worker_log,stderr=worker_log)
        def wait(client,job,states):
            deadline=time.monotonic()+150
            while time.monotonic()<deadline:
                assert worker.poll() is None,(base/'worker.log').read_text()
                value=client.get('/api/jobs/'+job).json
                with queue_db() as db:
                    assert db.execute("SELECT count(*) FROM queue WHERE state='running'").fetchone()[0]<=1
                if value['status'] in states:return value
                assert value['status']!='failed',value
                time.sleep(.1)
            raise AssertionError((base/'worker.log').read_text())
        try:
            pause=wait(two,second_id,('paused',));assert pause['completed_round']==4 and pause['recovered_failures']==1
            complete=wait(one,first_id,('complete',));assert complete['name']=='Renamed model'
            assert complete['recovered_failures']==0 # Demo flag alone cannot affect normal training.
            assert one.get('/api/custom/model').json['job_id']==first_id
            assert two.get('/api/custom/model').status_code==404
            assert one.post('/api/jobs/'+second_id+'/resume',headers=h1).status_code==404
            assert two.post('/api/jobs/'+second_id+'/resume',headers=h2).status_code==202
            complete2=wait(two,second_id,('complete',));assert complete2['resumed_from_round']==4 and complete2['recovered_failures']==1
            assert one.get('/api/custom/model').json['job_id']==first_id
            assert two.get('/api/custom/model').json['job_id']==second_id
            assert len(one.get('/api/jobs').json['jobs'])==1 and len(two.get('/api/jobs').json['jobs'])==1
            print('PASS: durable queue, one active job per user, serialized training, demo separation, worker recovery, checkpoints, resume, rename and account isolation',flush=True)
            info=one.get('/api/custom/model').json
            batch=one.post('/api/custom/predict-batch',data={'file':(io.BytesIO(sample),'sample.csv'),'model_version':info['model_version']},headers=h1)
            assert batch.status_code==200 and batch.headers['X-Prediction-Rows']=='40'
            assert b'predicted_outcome' in batch.data
            assert one.get('/api/custom/model/template').data.strip()==b'feature_1,feature_2'
            assert one.post('/api/custom/model/select',json={'job_id':second_id},headers=h1).status_code==404
            assert three.post('/api/custom/predict-batch',data={'file':(io.BytesIO(sample),'sample.csv')},headers=h3).status_code==404
            assert one.post('/api/custom/predict-batch',data={'file':(io.BytesIO(sample),'sample.csv'),'model_version':'stale'},headers=h1).status_code==409
            assert one.post('/api/train',data={'file':(io.BytesIO(b'a'*6*1024*1024),'large.csv')},headers=h1).status_code==413
            app.config['USER_STORAGE_BYTES']=1
            assert train(one,h1).status_code==400
            app.config['USER_STORAGE_BYTES']=100*1024*1024
            app.config['MAX_SAVED_JOBS']=1
            assert train(one,h1).status_code==400
            app.config['MAX_SAVED_JOBS']=100
            assert one.patch('/api/account',json={'name':'Updated name'},headers=h1).status_code==200
            assert one.get('/api/account').json['name']=='Updated name' and two.get('/api/account').json['name']!='Updated name'
            print('PASS: CSV-only download, prediction template, stale-model protection, upload/storage/job limits and private profile update',flush=True)
            third=train(three,h3);assert third.status_code==202
            third_id=third.json['job_id']
            wait(three,third_id,('training',))
            deadline=time.monotonic()+60
            while not checkpoint_path(third_id,owner3).exists() and time.monotonic()<deadline:time.sleep(.05)
            assert checkpoint_path(third_id,owner3).exists()
            assert three.post('/api/jobs/'+third_id+'/cancel',headers=h3).status_code==200
            cancelled_status=wait(three,third_id,('cancelled',))
            assert checkpoint_path(third_id,owner3).exists()
            assert three.post('/api/jobs/'+third_id+'/resume',headers=h3).status_code==202
            resumed=wait(three,third_id,('complete',));assert resumed['resumed_from_round']>=1
            assert one.delete('/api/jobs/'+first_id,json={'confirmation':'wrong'},headers=h1).status_code==400
            assert one.delete('/api/jobs/'+first_id,json={'confirmation':first_id},headers=h1).status_code==200
            assert not any(path.exists() for path in (checkpoint_path(first_id,owner1),upload_path(first_id,owner1),job_status_path(first_id,owner1),saved_model_path(first_id,owner1)))
            assert one.get('/api/custom/model').status_code==404
            assert two.get('/api/custom/model').json['job_id']==second_id
            print('PASS: queued/running cancellation, checkpoint retention, resume after cancellation, confirmed deletion and other users’ models preserved',flush=True)
        finally:
            worker.terminate()
            try:worker.wait(timeout=15)
            except subprocess.TimeoutExpired:worker.kill();worker.wait()
            worker_log.close()
            app.extensions['account_mail_executor'].shutdown(wait=True)

if __name__=='__main__':main()
