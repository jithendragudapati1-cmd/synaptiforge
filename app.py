import csv
import io
import json
import math
import os
import uuid
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from flask import Flask,g,jsonify,render_template,request,send_file
from werkzeug.exceptions import RequestEntityTooLarge

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
PRODUCTION=os.environ.get('SYNAPTIFORGE_ENV')=='production'
if not os.environ.get('APP_BASE_URL') and os.environ.get('RENDER_EXTERNAL_URL'):
    os.environ['APP_BASE_URL']=os.environ['RENDER_EXTERNAL_URL'].rstrip('/')
if PRODUCTION:
    if len(os.environ.get('SECRET_KEY',''))<32:raise RuntimeError('Production requires a SECRET_KEY of at least 32 characters.')
    origin=urlsplit(os.environ.get('APP_BASE_URL',''))
    if origin.scheme!='https' or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('','/'):
        raise RuntimeError('Production requires a valid HTTPS APP_BASE_URL origin.')
    if os.environ.get('COOKIE_SECURE','').lower()!='true':raise RuntimeError('Production requires COOKIE_SECURE=true.')
    if not Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR','')).is_absolute():raise RuntimeError('Production requires an absolute persistent storage directory.')
app=Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=6*1024*1024,
    UPLOAD_BYTES=int(os.environ.get('MAX_UPLOAD_MB','5'))*1024*1024,
    MAX_DATA_ROWS=int(os.environ.get('MAX_DATA_ROWS','10000')),
    MAX_DATA_FEATURES=int(os.environ.get('MAX_DATA_FEATURES','50')),
    USER_STORAGE_BYTES=int(os.environ.get('USER_STORAGE_MB','100'))*1024*1024,
    MAX_SAVED_JOBS=int(os.environ.get('MAX_SAVED_JOBS','100')),
    ALLOW_DEMOS=os.environ.get('ALLOW_DEMOS','false' if PRODUCTION else 'true').lower()=='true',
    SUPPORT_EMAIL=os.environ.get('SUPPORT_EMAIL') or os.environ.get('SMTP_USERNAME',''),
    BACKUP_RETENTION_DAYS=max(1,int(os.environ.get('BACKUP_RETENTION_DAYS','7'))))
# Bound the whole request before Flask parses uploaded files.
app.config['MAX_CONTENT_LENGTH']=app.config['UPLOAD_BYTES']+1024*1024
from auth_service import init_auth,EMAIL_RE
init_auth(app)
if app.config['SUPPORT_EMAIL'] and not EMAIL_RE.fullmatch(app.config['SUPPORT_EMAIL']):
    raise RuntimeError('Configure a valid SUPPORT_EMAIL address.')
if PRODUCTION and not app.config['SUPPORT_EMAIL']:
    raise RuntimeError('Production requires a support email address.')

@app.context_processor
def public_context():
    return {'support_email':app.config['SUPPORT_EMAIL']}

@app.get('/privacy')
def privacy_notice():
    return render_template('public_info.html',mode='privacy',retention=app.config['BACKUP_RETENTION_DAYS'])

@app.get('/support')
def support_page():
    return render_template('public_info.html',mode='support')

from custom_model_service import (get_custom_model_info,predict_custom,activate_custom_model,
    saved_model_path,active_job_id,predict_custom_batch)
from workspace_storage import jobs_directory,job_status_path,upload_path,checkpoint_path,model_root,write_json
from job_queue import (queue_db,enqueue,read_job,rename_job,cancel_job,delete_job,worker_ready,storage_used)
with queue_db():pass


def api(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        try:return function(*args,**kwargs)
        except FileNotFoundError as error:return jsonify(success=False,error=str(error)),404
        except RuntimeError as error:return jsonify(success=False,error=str(error)),409
        except (ValueError,TypeError,UnicodeError,csv.Error) as error:return jsonify(success=False,error=str(error)),400
    return wrapped


@app.errorhandler(RequestEntityTooLarge)
def too_large(error):
    return jsonify(success=False,error=f"Uploads must be {app.config['UPLOAD_BYTES']//1024//1024} MB or smaller."),413


@app.errorhandler(500)
def server_error(error):
    return jsonify(success=False,error='Something went wrong. Your saved work is preserved. Please try again.'),500


def limits():
    return {'storage_bytes':app.config['USER_STORAGE_BYTES'],'max_jobs':app.config['MAX_SAVED_JOBS']}


def dataset_upload():
    file=request.files.get('file')
    if not file or not file.filename or not file.filename.lower().endswith('.csv'):raise ValueError('Choose a CSV file.')
    content=file.stream.read(app.config['UPLOAD_BYTES']+1)
    if len(content)>app.config['UPLOAD_BYTES']:raise RequestEntityTooLarge()
    text=content.decode('utf-8-sig')
    headers=next(csv.reader(io.StringIO(text)),[])
    if not headers or any(not h.strip() or len(h)>80 or h[0] in '=+-@\t\r\n' for h in headers) or len(set(headers))!=len(headers):raise ValueError('Give every CSV column a unique, nonempty name.')
    if len(headers)>app.config['MAX_DATA_FEATURES']+1:raise ValueError(f"Use at most {app.config['MAX_DATA_FEATURES']} input columns and one target column.")
    data=pd.read_csv(io.StringIO(text),nrows=app.config['MAX_DATA_ROWS']+1)
    if data.empty:raise ValueError('The CSV contains no data rows.')
    if len(data)>app.config['MAX_DATA_ROWS']:raise ValueError(f"Use at most {app.config['MAX_DATA_ROWS']:,} rows.")
    return file,content,data


def validate_training(data,target,workers):
    if workers not in (1,2,4):raise ValueError('Choose 1, 2, or 4 workers.')
    if target not in data.columns:raise ValueError('Choose the column you want to predict.')
    if len(data)<10:raise ValueError('Training needs at least 10 rows.')
    features=data.drop(columns=[target])
    if features.empty or not 1<=features.shape[1]<=app.config['MAX_DATA_FEATURES']:raise ValueError('Include at least one numeric input column.')
    if features.select_dtypes(exclude='number').shape[1]:raise ValueError('All input columns must contain numbers. The target may contain two text labels.')
    if data.isna().any().any() or not np.isfinite(features.to_numpy(dtype=float)).all():raise ValueError('Fill missing values and remove infinite values before uploading.')
    if data[target].astype(str).str.len().max()>120:raise ValueError('Keep target labels under 120 characters.')
    counts=data[target].value_counts()
    if len(counts)!=2 or counts.min()<2:raise ValueError('The target must have exactly two possible values, with at least two rows for each.')
    if len(data)-math.ceil(.2*len(data))<workers:raise ValueError('Use more rows or fewer workers.')


def private_jobs():
    owner=g.user['id']
    try:selected=active_job_id()
    except FileNotFoundError:selected=None
    jobs=[]
    for path in jobs_directory().glob('*.json'):
        try:
            job=read_job(owner,path.stem)
            if job['status']=='deleted':continue
            has_checkpoint=checkpoint_path(path.stem).is_file()
            job.update(checkpoint=has_checkpoint,can_resume=job['status'] in ('paused','cancelled') and has_checkpoint,
                can_cancel=job['status'] in ('queued','starting','training','cancelling'),
                can_delete=job['status'] not in ('queued','starting','training','cancelling'),
                can_select_model=job['status']=='complete' and saved_model_path(path.stem).is_file(),
                model_selected=path.stem==selected)
            # No private filesystem paths or worker process IDs in browser responses.
            for key in ('csv_path','model_path','checkpoint_path','pid','owner_id'):job.pop(key,None)
            jobs.append(job)
        except (ValueError,OSError,TypeError,KeyError):continue
    jobs.sort(key=lambda job:job['created_at'],reverse=True)
    return jobs[:app.config['MAX_SAVED_JOBS']]


@app.get('/healthz')
def health():return jsonify(status='ok',training_available=worker_ready())


@app.get('/')
def dashboard():return render_template('index.html',demo_available=app.config['ALLOW_DEMOS'])


@app.get('/api/status')
@api
def status():
    complete=next((job for job in private_jobs() if job['status']=='complete'),None)
    return jsonify(success=True,latest=complete,training_available=worker_ready())


@app.post('/api/upload')
@api
def upload_dataset():
    file,content,data=dataset_upload()
    return jsonify(success=True,filename=Path(file.filename).name,rows=len(data),columns_count=len(data.columns),
                   columns=list(data.columns),numeric_columns=list(data.select_dtypes(include='number').columns))


@app.post('/api/train')
@api
def start_training():
    file,content,data=dataset_upload()
    target=request.form.get('target','')
    workers=int(request.form.get('workers','2'))
    validate_training(data,target,workers)
    demo=app.config['ALLOW_DEMOS'] and request.form.get('demo_mode')=='true'
    job=uuid.uuid4().hex[:8]
    name=request.form.get('name','').strip() or target+' model'
    if len(name)>80:raise ValueError('Use a job name of at most 80 characters.')
    value={'target':target,'workers':workers,'rounds':10,'name':name,
           'filename':Path(file.filename).name[:120],
           'simulate_failure':demo and request.form.get('simulate_failure')=='true',
           'checkpoint_demo':demo and request.form.get('checkpoint_demo')=='true'}
    if value['simulate_failure'] and workers!=4:raise ValueError('The worker-failure demo requires four workers.')
    enqueue(g.user['id'],job,value,content=content,limits=limits())
    return jsonify(success=True,job_id=job,status='queued'),202


@app.get('/api/jobs')
@api
def training_history():return jsonify(success=True,jobs=private_jobs())


@app.get('/api/jobs/<job>')
@api
def training_job_status(job):
    value=read_job(g.user['id'],job)
    for key in ('csv_path','checkpoint_path','model_path','pid','owner_id'):value.pop(key,None)
    return jsonify(value)


@app.post('/api/jobs/<job>/resume')
@api
def resume_training_job(job):
    value=read_job(g.user['id'],job)
    if value['status'] not in ('paused','cancelled'):raise RuntimeError('Only paused or cancelled jobs can resume.')
    if not checkpoint_path(job).is_file() or not upload_path(job).is_file():raise FileNotFoundError('The checkpoint or original upload is unavailable.')
    enqueue(g.user['id'],job,value,resume=True,limits=limits())
    return jsonify(success=True,job_id=job,status='queued'),202


@app.patch('/api/jobs/<job>')
@api
def rename_training_job(job):
    data=request.get_json(silent=True) or {}
    rename_job(g.user['id'],job,data.get('name',''))
    return jsonify(success=True)


@app.post('/api/jobs/<job>/cancel')
@api
def cancel_training_job(job):return jsonify(success=True,status=cancel_job(g.user['id'],job))


@app.delete('/api/jobs/<job>')
@api
def delete_training_job(job):
    read_job(g.user['id'],job)
    data=request.get_json(silent=True) or {}
    if data.get('confirmation')!=job:raise ValueError('Confirm deletion of this job before proceeding.')
    delete_job(g.user['id'],job)
    return jsonify(success=True)


@app.post('/api/custom/model/select')
@api
def select_custom_model():
    data=request.get_json(silent=True) or {}
    job=data.get('job_id')
    value=read_job(g.user['id'],job)
    if value['status']!='complete':raise RuntimeError('Choose a completed job.')
    activate_custom_model(job)
    return jsonify(get_custom_model_info())


@app.get('/api/custom/model')
@api
def custom_model_info():
    info=get_custom_model_info()
    if info.get('job_id'):info['name']=read_job(g.user['id'],info['job_id'])['name']
    return jsonify(info)


@app.get('/api/custom/model/template')
@api
def prediction_template():
    info=get_custom_model_info()
    buffer=io.StringIO();csv.writer(buffer).writerow(info['features'])
    return send_file(io.BytesIO(buffer.getvalue().encode()),mimetype='text/csv',as_attachment=True,download_name='prediction_template.csv')


@app.get('/api/sample/training')
def training_sample():
    buffer=io.StringIO();writer=csv.writer(buffer);writer.writerow(['feature_1','feature_2','outcome'])
    for index in range(40):writer.writerow([index,index%7,0 if index<20 else 1])
    return send_file(io.BytesIO(buffer.getvalue().encode()),mimetype='text/csv',as_attachment=True,download_name='sample_training.csv')


@app.post('/api/custom/predict-batch')
@api
def custom_predict_batch():
    file,content,data=dataset_upload()
    output,model_job=predict_custom_batch(data,request.form.get('model_version'))
    for column in output.columns:
        output[column]=output[column].map(lambda value: "'"+value if isinstance(value,str) and value.startswith(('=','+','-','@','\t','\r')) else value)
    result=send_file(io.BytesIO(output.to_csv(index=False).encode()),mimetype='text/csv',as_attachment=True,download_name='synaptiforge_predictions.csv')
    result.headers['X-Prediction-Rows']=str(len(output));result.headers['X-Model-Job']=model_job or 'legacy'
    return result


# Keep the authenticated CSV-model API available, without a single-row website form.
@app.post('/api/custom/predict')
@api
def custom_predict():
    data=request.get_json(silent=True)
    if not isinstance(data,dict) or 'features' not in data:raise ValueError('Request must contain a features list.')
    return jsonify(predict_custom(data['features'],data.get('model_version')))


@app.get('/api/account')
@api
def account_info():
    return jsonify(success=True,name=g.user['name'],email=g.user['email'],verified=bool(g.user['email_verified']),
                   google_linked=bool(g.user['google_sub']),storage_used=storage_used(g.user['id']),
                   storage_limit=app.config['USER_STORAGE_BYTES'],upload_mb=app.config['UPLOAD_BYTES']//1024//1024,
                   max_rows=app.config['MAX_DATA_ROWS'],max_features=app.config['MAX_DATA_FEATURES'],max_jobs=app.config['MAX_SAVED_JOBS'])


@app.patch('/api/account')
@api
def update_account():
    data=request.get_json(silent=True) or {};name=str(data.get('name','')).strip()
    if not 1<=len(name)<=80:raise ValueError('Use a name between 1 and 80 characters.')
    from auth_service import database
    with database() as db:db.execute('UPDATE users SET name=? WHERE id=?',(name,g.user['id']))
    return jsonify(success=True,name=name)

@app.get('/api/account/export')
@api
def download_account_data():
    from account_management import export_account
    stream=export_account(g.user['id'])
    result=send_file(stream,mimetype='application/zip',as_attachment=True,download_name='synaptiforge-account-data.zip')
    result.call_on_close(stream.close)
    return result


@app.delete('/api/account')
@api
def remove_account():
    from account_management import delete_account
    data=request.get_json(silent=True) or {}
    delete_account(g.user['id'],data.get('confirmation'),data.get('password'))
    return jsonify(success=True,message='Your account and live workspace have been deleted.')


if __name__=='__main__':
    app.run(host='127.0.0.1',port=5000,debug=False)
