"""Start the local web app, training queue, and email retry worker together."""
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
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    children=[subprocess.Popen([sys.executable,str(ROOT/name)],cwd=str(ROOT)) for name in ('app.py','task_worker.py','mail_worker.py','backup_worker.py')]
    try:
        while not STOP:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A local service stopped. Restart with python run_local.py.')
            time.sleep(.5)
    finally:
        for child in children:
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()

if __name__=='__main__':main()
