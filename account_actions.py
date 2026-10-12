"""Single-use email verification and password recovery."""
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from cryptography.fernet import Fernet
from flask import current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash
from auth_service import bp, database, limited, welcome_email, send_account_message


def cipher():
    secret = current_app.secret_key.encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def deliver_message(message_id):
    with database() as db:
        claimed = db.execute("UPDATE account_mail SET status='sending',updated=? WHERE id=? AND status='pending'", (int(time.time()), message_id)).rowcount
        if not claimed:
            return
        row = db.execute('SELECT * FROM account_mail WHERE id=?', (message_id,)).fetchone()
    if row['expires'] < int(time.time()):
        with database() as db:
            db.execute("UPDATE account_mail SET status='expired',payload='' WHERE id=?", (message_id,))
        return
    try:
        content = json.loads(cipher().decrypt(row['payload'].encode()))
        raw_match = re.search(r'token=([A-Za-z0-9_-]+)', content['body'])
        digest = hashlib.sha256(raw_match.group(1).encode()).hexdigest() if raw_match else ''
        with database() as db:
            active = db.execute('SELECT 1 FROM account_tokens WHERE digest=? AND used=0 AND expires>?',
                                (digest, int(time.time()))).fetchone()
            if not active:
                db.execute("UPDATE account_mail SET status='expired',payload='' WHERE id=?", (message_id,))
                return
        sent = send_account_message(content['email'], content['subject'], content['body'])
    except Exception:
        sent = False
    with database() as db:
        db.execute('UPDATE account_mail SET status=?,payload=?,updated=? WHERE id=?',
                   ('sent' if sent else 'pending', '' if sent else row['payload'], int(time.time()), message_id))


def background(function, *args):
    app = current_app._get_current_object()
    def run():
        with app.app_context():
            try:
                function(*args)
            except Exception:
                app.logger.warning('Account email operation could not complete; retry is available.')
    if app.config.get('ACCOUNT_MAIL_INLINE_TEST'):
        run()
    else:
        app.extensions['account_mail_executor'].submit(run)


def request_link(user_id, purpose):
    now = int(time.time())
    duration = 86400 if purpose == 'verify' else 1800
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with database() as db:
        user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not user or (purpose == 'verify' and user['email_verified']):
            return
        if purpose == 'reset' and not user['password_hash']:
            return
        recent = db.execute('SELECT max(created) FROM account_tokens WHERE user_id=? AND purpose=?', (user_id, purpose)).fetchone()[0]
        if recent and now - recent < 60:
            return
        db.execute('UPDATE account_tokens SET used=1 WHERE user_id=? AND purpose=?', (user_id, purpose))
        db.execute('INSERT INTO account_tokens(digest,user_id,purpose,created,expires,used) VALUES (?,?,?,?,?,0)', (digest,user_id,purpose,now,now+duration))
        route = '/verify-email' if purpose == 'verify' else '/reset-password'
        link = current_app.config['APP_BASE_URL'] + route + '?token=' + raw
        subject = 'Verify your Synaptiforge email' if purpose == 'verify' else 'Reset your Synaptiforge password'
        action = 'verify your email address' if purpose == 'verify' else 'choose a new password'
        ttl = '24 hours' if purpose == 'verify' else '30 minutes'
        body = f'Hi {user["name"]},\n\nOpen this link to {action}:\n{link}\n\nThe link expires in {ttl} and can only be used once.\nIf you did not request this, you can ignore this email.\n\nThe Synaptiforge team\n'
        payload = cipher().encrypt(json.dumps({'email':user['email'],'subject':subject,'body':body}).encode()).decode()
        message_id = db.execute("INSERT INTO account_mail(payload,status,expires,updated,user_id) VALUES (?,'pending',?,?,?)", (payload,now+duration,now,user_id)).lastrowid
    deliver_message(message_id)


def find_token(raw, purpose, db):
    if not isinstance(raw,str) or not 20 <= len(raw) <= 100:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return db.execute('SELECT * FROM account_tokens WHERE digest=? AND purpose=? AND used=0 AND expires>?', (digest,purpose,int(time.time()))).fetchone()


@bp.get('/verification-pending')
def verification_pending():
    if not g.user:
        return redirect(url_for('auth.login'))
    if g.user['email_verified']:
        return redirect(url_for('dashboard'))
    return render_template('account_action.html', mode='pending')


@bp.post('/resend-verification')
def resend_verification():
    if not g.user:
        return redirect(url_for('auth.login'))
    if limited('verify-resend', 5):
        flash('Please wait before requesting another email.', 'error')
    else:
        background(request_link,g.user['id'],'verify')
        flash('Verification email requested. Check your inbox and Spam. Allow one minute between requests.', 'success')
    return redirect(url_for('auth.verification_pending'))


@bp.route('/verify-email',methods=['GET','POST'])
def verify_email():
    raw = request.form.get('token','') if request.method=='POST' else request.args.get('token','')
    with database() as db:
        row=find_token(raw,'verify',db)
        if row and request.method=='POST':
            changed=db.execute('UPDATE account_tokens SET used=1 WHERE digest=? AND used=0 AND expires>?', (row['digest'],int(time.time()))).rowcount
            if changed:
                db.execute('UPDATE users SET email_verified=1 WHERE id=?',(row['user_id'],))
    if not row or (request.method=='POST' and not changed):
        return render_template('account_action.html',mode='invalid'),400
    if request.method=='POST':
        background(welcome_email,row['user_id'])
        flash('Email verified. You can now log in and use your workspace.', 'success')
        return redirect(url_for('dashboard') if g.user and g.user['id']==row['user_id'] else url_for('auth.login'))
    return render_template('account_action.html',mode='verify',token=raw)


@bp.route('/forgot-password',methods=['GET','POST'])
def forgot_password():
    if request.method=='POST':
        if limited('password-reset',5):
            flash('Too many requests. Please try again in 15 minutes.', 'error')
            return render_template('account_action.html',mode='forgot'),429
        email=request.form.get('email','').strip().lower()[:254]
        # Perform the lookup and SMTP work away from the response path.
        def lookup_and_send():
            with database() as db:
                row=db.execute('SELECT id FROM users WHERE email=? AND password_hash IS NOT NULL',(email,)).fetchone()
            if row:
                request_link(row['id'],'reset')
        background(lookup_and_send)
        return render_template('account_action.html',mode='requested')
    return render_template('account_action.html',mode='forgot')


@bp.route('/reset-password',methods=['GET','POST'])
def reset_password():
    raw=request.form.get('token','') if request.method=='POST' else request.args.get('token','')
    with database() as db:
        row=find_token(raw,'reset',db)
    if not row:
        return render_template('account_action.html',mode='invalid'),400
    if request.method=='POST':
        password=request.form.get('password','')
        confirm=request.form.get('confirm_password','')
        if not 12 <= len(password) <= 128 or password != confirm:
            flash('Use 12–128 characters and make sure both passwords match.','error')
            return render_template('account_action.html',mode='reset',token=raw),400
        hashed=generate_password_hash(password)
        with database() as db:
            changed=db.execute('UPDATE account_tokens SET used=1 WHERE digest=? AND used=0 AND expires>?',(row['digest'],int(time.time()))).rowcount
            if changed:
                db.execute('UPDATE users SET password_hash=?,email_verified=1,session_version=session_version+1 WHERE id=?',(hashed,row['user_id']))
                db.execute('UPDATE account_tokens SET used=1 WHERE user_id=?',(row['user_id'],))
                user=db.execute('SELECT email FROM users WHERE id=?',(row['user_id'],)).fetchone()
        if not changed:
            return render_template('account_action.html',mode='invalid'),400
        background(send_account_message,user['email'],'Your Synaptiforge password was changed','Your password was reset. All previous sessions have been signed out. If this was not you, reset your password and contact the app owner.\n')
        session.clear()
        flash('Password updated. Log in with your new password.','success')
        return redirect(url_for('auth.login'))
    return render_template('account_action.html',mode='reset',token=raw)


def init_account_actions(app):
    with app.app_context(),database() as db:
        db.execute('BEGIN IMMEDIATE')
        columns={row[1] for row in db.execute('PRAGMA table_info(users)')}
        if 'email_verified' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0')
        if 'session_version' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0')
        db.execute('UPDATE users SET email_verified=1 WHERE google_sub IS NOT NULL')
        db.executescript('''CREATE TABLE IF NOT EXISTS account_tokens(digest TEXT PRIMARY KEY,user_id INTEGER NOT NULL,purpose TEXT NOT NULL,created INTEGER NOT NULL,expires INTEGER NOT NULL,used INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS account_mail(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,expires INTEGER NOT NULL,updated INTEGER NOT NULL);''')
    with app.app_context(),database() as db:
        db.execute('BEGIN IMMEDIATE')
        if 'user_id' not in {row[1] for row in db.execute('PRAGMA table_info(account_mail)')}:
            db.execute('ALTER TABLE account_mail ADD COLUMN user_id INTEGER')
    app.extensions['account_mail_executor']=ThreadPoolExecutor(max_workers=2,thread_name_prefix='account-mail')
    # Keep bearer links out of the development server's access logs.
    class RedactTokens(logging.Filter):
        def filter(self,record):
            record.msg=re.sub(r'(token=)[^&\s"]+',r'\1[redacted]',record.getMessage())
            record.args=()
            return True
    logging.getLogger('werkzeug').addFilter(RedactTokens())
    @app.cli.command('retry-account-emails')
    def retry_account_emails():
        with database() as db:
            db.execute("UPDATE account_mail SET status='pending' WHERE status='sending' AND updated<?",(int(time.time())-600,))
            rows=db.execute("SELECT id FROM account_mail WHERE status='pending'").fetchall()
        for row in rows:deliver_message(row['id'])
        import click
        click.echo('Account email queue processed. Failed deliveries remain pending.')
