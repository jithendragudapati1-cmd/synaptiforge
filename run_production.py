"""Small-beta production supervisor. Share one persistent volume across processes."""
from pathlib import Path
import os
import signal
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parent
STOP=False

def stop(signum,frame):
    global STOP
    STOP=True

def main():
    os.chdir(ROOT)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    port=os.environ.get('PORT','8080')
    commands=[
      [sys.executable,'-m','gunicorn','--bind','0.0.0.0:'+port,'--workers','1','--threads','4','--timeout','60','--graceful-timeout','20','--access-logfile','-',
       '--access-logformat','%(h)s %(m)s %(U)s %(s)s','app:app'],
      [sys.executable,str(ROOT/'task_worker.py')],
      [sys.executable,str(ROOT/'mail_worker.py')],
      [sys.executable,str(ROOT/'backup_worker.py')]]
    children=[subprocess.Popen(command,cwd=str(ROOT)) for command in commands]
    exit_code=0
    try:
        while not STOP:
            if any(child.poll() is not None for child in children):
                print('A production service stopped; shutting down for a clean restart.',file=sys.stderr)
                exit_code=1;break
            time.sleep(.5)
    finally:
        for child in children:
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.wait(timeout=25)
            except subprocess.TimeoutExpired:child.kill();child.wait()
    return exit_code

if __name__=='__main__':sys.exit(main())
