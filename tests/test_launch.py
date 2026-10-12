"""Production origin, public notices, headers and backup-worker startup checks."""
import json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='synaptiforge_launch_') as folder:
        base=Path(folder)
        for path in ROOT.glob('*.py'):shutil.copy2(path,base/path.name)
        for name in ('templates','static'):shutil.copytree(ROOT/name,base/name)
        os.chdir(base);sys.path.insert(0,str(base))
        os.environ.update(SYNAPTIFORGE_ENV='production',SECRET_KEY='launch-test-secret-with-more-than-32-characters',COOKIE_SECURE='true',SYNAPTIFORGE_STORAGE_DIR=str(base/'storage'),AUTH_DATA_DIR=str(base/'storage/data/auth'),SUPPORT_EMAIL='support@example.com',SMTP_USERNAME='',SMTP_PASSWORD='',GOOGLE_CLIENT_ID='test-client',GOOGLE_CLIENT_SECRET='test-secret',RENDER_EXTERNAL_URL='https://synaptiforge-test.example',ALLOW_DEMOS='false')
        os.environ.pop('APP_BASE_URL',None)
        from app import app
        app.config.update(TESTING=True,ACCOUNT_MAIL_INLINE_TEST=True)
        from auth_service import create_account
        import account_actions
        assert app.config['APP_BASE_URL']=='https://synaptiforge-test.example'
        client=app.test_client()
        for route in ('/privacy','/support'):
            result=client.get(route);assert result.status_code==200
            assert 'support@example.com' in result.get_data(as_text=True) if route=='/support' else True
            assert result.headers['Strict-Transport-Security']=='max-age=31536000'
        result=client.get('/login');assert 'Secure' in result.headers['Set-Cookie']
        assert client.get('/api/jobs').status_code==401
        google=app.extensions['synaptiforge_google']
        with patch.object(google,'load_server_metadata',return_value={'authorization_endpoint':'https://accounts.google.com/o/oauth2/v2/auth'}):
            response=client.get('/auth/google')
            assert parse_qs(urlparse(response.location).query)['redirect_uri']==['https://synaptiforge-test.example/auth/google/callback']
        messages=[]
        with app.app_context(),patch.object(account_actions,'send_account_message',side_effect=lambda email,subject,body: messages.append(body) or True):
            uid=create_account('Test','verify@example.com',password_hash='test-hash')
            account_actions.request_link(uid,'verify')
        assert 'https://synaptiforge-test.example/verify-email?token=' in messages[0]
        run=subprocess.run([sys.executable,str(base/'backup_worker.py'),'--once'],cwd=base,env=os.environ.copy(),capture_output=True,text=True,timeout=20)
        assert run.returncode==0,run.stderr
        backups=base/'storage/backups/automatic'
        assert json.loads((backups/'status.json').read_text())['status']=='ok'
        assert len(list(backups.glob('snapshot-*.sfb')))==1
        print('PASS public notices and support, HTTPS/HSTS/secure cookies, automatic Render origin for OAuth/email, and backup-worker startup')
        app.extensions['account_mail_executor'].shutdown(wait=True)
        environment=os.environ.copy();environment['APP_BASE_URL']='http://example.invalid'
        run=subprocess.run([sys.executable,'-c','import app'],cwd=base,env=environment,capture_output=True,timeout=10)
        assert run.returncode!=0
        print('PASS refuses an insecure production origin')

if __name__=='__main__':main()
