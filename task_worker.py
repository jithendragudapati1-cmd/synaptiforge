"""Run queued training in guarded .py subprocesses; one coordinator per volume."""
from pathlib import Path
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parent
STOP=False


def stop(signum,frame):
    global STOP
    STOP=True


def alive(pid):
    if not pid:return False
    try:
        os.kill(pid,0);return True
    except OSError:return False


def terminate_group(pid):
    if not alive(pid):return
    try:
        if os.getpgid(pid)==pid:os.killpg(pid,signal.SIGTERM)
    except ProcessLookupError:pass


def run_one(row):
    from job_queue import queue_db,read_job,heartbeat
    from workspace_storage import job_status_path,checkpoint_path,write_json,upload_path
    owner,job=row['owner'],row['job']
    status=read_job(owner,job)
    process=None
    pid=row['pid']
    if row['state']=='queued':
        command=[sys.executable,str(ROOT/'training_job.py'),str(upload_path(job,owner)),status['target'],str(status['workers']),job,
                 'true' if status.get('simulate_failure') else 'false','--owner-id',str(owner)]
        if row['resume']:command.append('--resume')
        elif status.get('checkpoint_demo'):command.extend(['--stop-after-round','4'])
        with queue_db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE queue SET state='running' WHERE id=? AND state='queued' AND cancel=0",(row['id'],)).rowcount
        if not changed:return
        status.update(status='starting',message='Starting the next training job.')
        write_json(job_status_path(job,owner),status)
        env=os.environ.copy()
        for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):env[key]='1'
        process=subprocess.Popen(command,cwd=str(ROOT),env=env,start_new_session=True)
        pid=process.pid
        with queue_db() as db:db.execute('UPDATE queue SET pid=? WHERE id=?',(pid,row['id']))
    else:
        # Adopt a surviving child after a coordinator restart; never launch it twice.
        pid=pid or status.get('pid')
    started=time.monotonic()
    cancelled=False
    timed_out=False
    while alive(pid) and (process is None or process.poll() is None):
        heartbeat('training')
        with queue_db() as db:current=db.execute('SELECT cancel FROM queue WHERE id=?',(row['id'],)).fetchone()
        cancelled=bool(current and current['cancel'])
        timed_out=time.monotonic()-started>int(os.environ.get('MAX_TRAIN_SECONDS','300'))
        if cancelled or STOP or timed_out:
            terminate_group(pid)
            if process:
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:os.killpg(pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait(timeout=5)
            break
        if process is None:
            state=json.loads(job_status_path(job,owner).read_text()).get('status')
            if state in ('complete','paused','failed','cancelled'):break
        time.sleep(.25)
    if process:process.wait(timeout=10)
    final=json.loads(job_status_path(job,owner).read_text())
    if cancelled and final.get('status')!='complete':
        final.update(status='cancelled',success=True,message='Training cancelled. Saved checkpoints are kept.')
    elif timed_out or STOP or final.get('status') not in ('complete','paused','failed','cancelled'):
        final.update(status='paused' if checkpoint_path(job,owner).is_file() else 'failed',
                     success=checkpoint_path(job,owner).is_file(),message='Training stopped. Resume its saved checkpoint.' if checkpoint_path(job,owner).is_file() else 'Training stopped before a checkpoint was saved.')
    write_json(job_status_path(job,owner),final)
    with queue_db() as db:db.execute('UPDATE queue SET state=?,pid=NULL WHERE id=?',(final['status'],row['id']))


def main():
    os.chdir(ROOT);load_dotenv(ROOT/'.env')
    from job_queue import queue_db,queue_path,heartbeat
    from workspace_storage import private_directory
    private_directory(queue_path().parent)
    lock=(queue_path().parent/'training-worker.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print('A training worker already manages this storage volume.');return
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    while not STOP:
        heartbeat('training')
        with queue_db() as db:
            row=db.execute("SELECT * FROM queue WHERE state IN ('running','cancelling') ORDER BY id LIMIT 1").fetchone()
            if not row:row=db.execute("SELECT * FROM queue WHERE state='queued' ORDER BY created,id LIMIT 1").fetchone()
        if row:
            try:run_one(dict(row))
            except Exception:
                import logging
                logging.exception('Training worker failed to process a queued job.')
                from workspace_storage import job_status_path,write_json
                try:
                    value=json.loads(job_status_path(row['job'],row['owner']).read_text())
                    value.update(status='failed',success=False,error='The training worker could not complete this job.')
                    write_json(job_status_path(row['job'],row['owner']),value)
                    with queue_db() as db:db.execute("UPDATE queue SET state='failed',pid=NULL WHERE id=?",(row['id'],))
                except Exception:pass
        else:time.sleep(.5)
    with queue_db() as db:db.execute("DELETE FROM service_state WHERE name='training'")

if __name__=='__main__':main()
