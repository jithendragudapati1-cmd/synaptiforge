"""Local accounts, Google OIDC login, and a durable welcome-email outbox."""
from contextlib import contextmanager
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
import hmac
import fcntl
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from urllib.parse import urlparse, parse_qs

import click
import certifi
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (Blueprint, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint('auth', __name__)
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")


@contextmanager
def database():
    connection = sqlite3.connect(current_app.config['AUTH_DATABASE'], timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def csrf_token():
    return session.setdefault('csrf_token', secrets.token_urlsafe(32))


def normalize_email(value):
    value = value.strip().lower()
    if len(value) > 254 or not EMAIL_RE.fullmatch(value):
        raise ValueError('Enter a valid email address.')
    return value


def limited(action, maximum):
    now = int(time.time())
    key = action + ':' + (request.remote_addr or 'local')
    with database() as db:
        db.execute('DELETE FROM attempts WHERE created < ?', (now - 900,))
        count = db.execute('SELECT count(*) FROM attempts WHERE bucket=?', (key,)).fetchone()[0]
        if count >= maximum:
            return True
        db.execute('INSERT INTO attempts(bucket,created) VALUES (?,?)', (key, now))
    return False


def create_account(name, email, password_hash=None, google_sub=None):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT count(*) FROM users').fetchone()[0]>=int(os.environ.get('MAX_ACCOUNTS','50')):
            raise ValueError('This beta has reached its account limit. Please contact support.')
        # Never reuse an ID: old sessions and retained files must not belong to a new user.
        next_id=max(db.execute('SELECT next_id FROM account_sequence WHERE singleton=1').fetchone()[0],db.execute('SELECT coalesce(max(id),0)+1 FROM users').fetchone()[0])
        db.execute('UPDATE account_sequence SET next_id=? WHERE singleton=1',(next_id+1,))
        cursor = db.execute('INSERT INTO users(id,name,email,password_hash,google_sub,created,email_verified) VALUES (?,?,?,?,?,?,?)',
                            (next_id,name, email, password_hash, google_sub, int(time.time()), int(google_sub is not None)))
        user_id = cursor.lastrowid
        db.execute("INSERT INTO mail_outbox(user_id,status,updated) VALUES (?,'pending',?)", (user_id, int(time.time())))
    return user_id


def welcome_email(user_id):
    """Attempt once; keep unsent mail queued. SMTP acceptance is not inbox delivery."""
    config = current_app.config
    if not config.get('SMTP_USERNAME') or not config.get('SMTP_PASSWORD'):
        return False
    with database() as db:
        db.execute("UPDATE mail_outbox SET status='pending' WHERE status='sending' AND updated < ?", (int(time.time()) - 600,))
        claimed = db.execute("UPDATE mail_outbox SET status='sending',updated=? WHERE user_id=? AND status='pending'",
                             (int(time.time()), user_id)).rowcount
        if not claimed:
            return False
        user = db.execute('SELECT name,email,email_verified FROM users WHERE id=?', (user_id,)).fetchone()
        if not user['email_verified']:
            db.execute("UPDATE mail_outbox SET status='pending' WHERE user_id=?", (user_id,))
            return False
    message = EmailMessage()
    message['Subject'] = 'Welcome to Synaptiforge'
    message['From'] = config['SMTP_USERNAME']
    message['To'] = user['email']
    message['Message-ID'] = f'<synaptiforge-welcome-{user_id}@{config["SMTP_USERNAME"].split("@")[-1]}>'
    message.set_content(f'Hi {user["name"]},\n\nYour Synaptiforge account has been created.\n\n'
                        'You can now train models, recover failed workers, resume checkpoints, and make predictions.\n\n'
                        f'Open Synaptiforge: {config["APP_BASE_URL"]}\n\n'
                        'Your training jobs and models are private to your account.\n\nThe Synaptiforge team\n')
    if not send_account_message(user['email'], message['Subject'], message.get_content()):
        with database() as db:
            db.execute("UPDATE mail_outbox SET status='pending',updated=? WHERE user_id=?", (int(time.time()), user_id))
        return False
    with database() as db:
        db.execute("UPDATE mail_outbox SET status='sent',updated=? WHERE user_id=?", (int(time.time()), user_id))
    return True


def send_account_message(email, subject, body):
    config = current_app.config
    if not config.get('SMTP_USERNAME') or not config.get('SMTP_PASSWORD'):
        return False
    message = EmailMessage()
    message['Subject'], message['From'], message['To'] = subject, config['SMTP_USERNAME'], email
    message.set_content(body)
    try:
        with smtplib.SMTP(config['SMTP_HOST'], config['SMTP_PORT'], timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context(cafile=certifi.where()))
            smtp.ehlo()
            password = config['SMTP_PASSWORD']
            if config['SMTP_HOST'] == 'smtp.gmail.com':
                password = ''.join(password.split())
            smtp.login(config['SMTP_USERNAME'], password)
            if smtp.send_message(message):
                raise smtplib.SMTPException('Recipient refused')
        return True
    except (smtplib.SMTPException, OSError, ValueError):
        current_app.logger.warning('Account email delivery failed; no credentials or link were logged.')
        return False


def sign_in(user_id):
    session.clear()
    session['user_id'] = user_id
    session['reauthenticated_at'] = int(time.time())
    with database() as db:
        session['session_version'] = db.execute('SELECT session_version FROM users WHERE id=?', (user_id,)).fetchone()[0]
    session.permanent = True
    csrf_token()


@bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if g.user:
        return redirect(url_for('dashboard') if g.user['email_verified'] else url_for('auth.verification_pending'))
    if request.method == 'POST':
        if limited('signup', 5):
            flash('Too many sign-up attempts. Try again in 15 minutes.', 'error')
            return render_template('auth.html', mode='signup'), 429
        try:
            name = request.form.get('name', '').strip()
            if not 1 <= len(name) <= 80:
                raise ValueError('Enter a name between 1 and 80 characters.')
            email = normalize_email(request.form.get('email', ''))
            password = request.form.get('password', '')
            if not 12 <= len(password) <= 128:
                raise ValueError('Use a password between 12 and 128 characters.')
            if password != request.form.get('confirm_password', ''):
                raise ValueError('The passwords do not match.')
            user_id = create_account(name, email, generate_password_hash(password))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else 'This email is already registered. Log in instead.', 'error')
            return render_template('auth.html', mode='signup'), 400
        from account_actions import background, request_link
        sign_in(user_id)
        background(request_link, user_id, 'verify')
        flash('Account created. Check your inbox to verify your email before using the workspace.', 'success')
        return redirect(url_for('auth.verification_pending'))
    return render_template('auth.html', mode='signup')


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('dashboard') if g.user['email_verified'] else url_for('auth.verification_pending'))
    if request.method == 'POST':
        if limited('login', 10):
            flash('Too many login attempts. Try again in 15 minutes.', 'error')
            return render_template('auth.html', mode='login'), 429
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        with database() as db:
            user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        candidate = user['password_hash'] if user and user['password_hash'] else current_app.config['DUMMY_PASSWORD_HASH']
        valid = len(password) <= 128 and check_password_hash(candidate, password)
        if not user or not user['password_hash'] or not valid:
            flash('Email or password is incorrect. Google accounts should use Continue with Google.', 'error')
            return render_template('auth.html', mode='login'), 401
        sign_in(user['id'])
        if not user['email_verified']:
            from account_actions import background, request_link
            background(request_link, user['id'], 'verify')
            return redirect(url_for('auth.verification_pending'))
        return redirect(url_for('dashboard'))
    return render_template('auth.html', mode='login')


@bp.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@bp.get('/auth/google')
def google_login():
    session.pop("google_link", None)
    if not current_app.config['GOOGLE_ENABLED']:
        flash('Google sign-in needs setup. You can use email and password now.', 'error')
        return redirect(url_for('auth.login'))
    google = current_app.extensions['synaptiforge_google']
    return google.authorize_redirect(current_app.config['APP_BASE_URL'] + '/auth/google/callback', prompt='select_account')


@bp.post('/auth/google/link')
def google_link():
    if not current_app.config['GOOGLE_ENABLED']:
        flash('Google sign-in needs configuration first.', 'error')
        return redirect(url_for('dashboard'))
    google = current_app.extensions['synaptiforge_google']
    response = google.authorize_redirect(current_app.config['APP_BASE_URL'] + '/auth/google/callback', prompt='select_account')
    state = parse_qs(urlparse(response.location).query)['state'][0]
    session['google_link'] = {'user_id': g.user['id'], 'state': state, 'created': int(time.time())}
    return response


@bp.get('/auth/google/callback')
def google_callback():
    if not current_app.config['GOOGLE_ENABLED']:
        return redirect(url_for('auth.login'))
    link = session.pop('google_link', None)
    try:
        if link and (not g.user or g.user['id'] != link['user_id']
                     or int(time.time()) - link['created'] > 600
                     or not hmac.compare_digest(request.args.get('state', ''), link['state'])):
            raise ValueError('The account-linking request expired.')
        # Authlib validates OAuth state and the signed OIDC token (including nonce).
        token = current_app.extensions['synaptiforge_google'].authorize_access_token()
        info = token.get('userinfo')
        if not info or not info.get('sub') or info.get('email_verified') is not True:
            raise ValueError('Google did not provide a verified email.')
        email = normalize_email(info['email'])
        with database() as db:
            user = db.execute('SELECT * FROM users WHERE google_sub=?', (info['sub'],)).fetchone()
            existing_email = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if link:
            if email != g.user['email']:
                flash('Choose the Google account with the same email as your Synaptiforge account.', 'error')
                return redirect(url_for('dashboard'))
            if user and user['id'] != g.user['id']:
                flash('This Google account is already linked to another account.', 'error')
                return redirect(url_for('dashboard'))
            with database() as db:
                changed = db.execute(
                    'UPDATE users SET google_sub=?,email_verified=1 WHERE id=? AND (google_sub IS NULL OR google_sub=?)',
                    (info['sub'], g.user['id'], info['sub'])).rowcount
                if changed != 1:
                    raise ValueError('This account is already linked to a different Google identity.')
            sign_in(g.user['id'])
            flash('Google account linked. You can now log in with Google or your password.', 'success')
            return redirect(url_for('dashboard'))
        created = user is None
        if created:
            # Never merge identities solely on matching email addresses.
            if existing_email:
                flash('An account already uses this email. Sign in with its original method.', 'error')
                return redirect(url_for('auth.login'))
            user_id = create_account(str(info.get('name') or email.split('@')[0])[:80], email, google_sub=info['sub'])
            sent = welcome_email(user_id)
        else:
            user_id = user['id']
        sign_in(user_id)
        if created:
            flash('Account created. Your welcome email was sent.' if sent else 'Account created. Your welcome email is queued.', 'success')
        return redirect(url_for('dashboard'))
    except Exception:
        # Do not expose OAuth tokens, authorization codes, or provider error detail.
        current_app.logger.warning('Google sign-in could not be completed.')
        flash('Google sign-in failed or was cancelled. Please try again.', 'error')
        return redirect(url_for('auth.login'))


def init_auth(app):
    root = Path(app.root_path)
    load_dotenv(root / '.env', override=False)
    auth_dir = Path(os.environ.get('AUTH_DATA_DIR', str(Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or root) / 'data/auth')))
    auth_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(auth_dir, 0o700)
    secret_file = auth_dir / 'session.key'
    secret = os.environ.get('SECRET_KEY')
    if not secret:
        # Web and email services can initialize together on the first local launch.
        with (auth_dir / 'session-key.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not secret_file.exists() or not secret_file.read_text().strip():
                secret_file.write_text(secrets.token_hex(32))
                os.chmod(secret_file, 0o600)
            secret = secret_file.read_text().strip()
    app.config.update(
        SECRET_KEY=secret, AUTH_DATABASE=str(auth_dir / 'accounts.sqlite3'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'false').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        APP_BASE_URL=os.environ.get('APP_BASE_URL', 'http://127.0.0.1:5000').rstrip('/'),
        GOOGLE_CLIENT_ID=os.environ.get('GOOGLE_CLIENT_ID', ''),
        GOOGLE_CLIENT_SECRET=os.environ.get('GOOGLE_CLIENT_SECRET', ''),
        SMTP_HOST=os.environ.get('SMTP_HOST', 'smtp.gmail.com'),
        SMTP_PORT=int(os.environ.get('SMTP_PORT', '587')),
        SMTP_USERNAME=os.environ.get('SMTP_USERNAME', ''),
        SMTP_PASSWORD=os.environ.get('SMTP_PASSWORD', ''),
        DUMMY_PASSWORD_HASH=generate_password_hash(secrets.token_urlsafe(32)),
    )
    app.config['GOOGLE_ENABLED'] = bool(app.config['GOOGLE_CLIENT_ID'] and app.config['GOOGLE_CLIENT_SECRET'])
    with app.app_context(), database() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            password_hash TEXT, google_sub TEXT UNIQUE, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS mail_outbox (
            user_id INTEGER PRIMARY KEY, status TEXT NOT NULL, updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (bucket TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS account_sequence(singleton INTEGER PRIMARY KEY,next_id INTEGER NOT NULL);
        INSERT OR IGNORE INTO account_sequence SELECT 1,coalesce(max(id),0)+1 FROM users;
        ''')
    os.chmod(app.config['AUTH_DATABASE'], 0o600)
    from account_actions import init_account_actions
    init_account_actions(app)
    oauth = OAuth(app)
    app.extensions['synaptiforge_google'] = oauth.register(
        'google', client_id=app.config['GOOGLE_CLIENT_ID'], client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile', 'code_challenge_method': 'S256', 'timeout': 10})
    app.register_blueprint(bp)
    app.jinja_env.globals['csrf_token'] = csrf_token

    @app.context_processor
    def auth_context():
        return {'current_user': getattr(g, 'user', None), 'google_enabled': app.config['GOOGLE_ENABLED']}

    @app.before_request
    def protect_workspace():
        g.user = None
        user_id = session.get('user_id')
        if user_id:
            with database() as db:
                row = db.execute('SELECT id,name,email,google_sub,email_verified,session_version FROM users WHERE id=?', (user_id,)).fetchone()
                if row and row['session_version'] == session.get('session_version', 0):
                    g.user = dict(row)
                else:
                    session.clear()
        if g.user:
            from account_management import lock_account
            g.account_lock=lock_account(g.user['id'])
            from job_queue import queue_db
            with queue_db() as queue:
                deleted=queue.execute('SELECT 1 FROM deleted_accounts WHERE owner=?',(g.user['id'],)).fetchone()
            with database() as db:
                row=db.execute('SELECT id,name,email,google_sub,email_verified,session_version FROM users WHERE id=?',(g.user['id'],)).fetchone()
                if deleted or not row or row['session_version']!=session.get('session_version',0):
                    g.user=None;session.clear()
        public = request.endpoint in {'auth.login', 'auth.signup', 'auth.google_login', 'auth.google_callback', 'auth.verification_pending', 'auth.resend_verification', 'auth.verify_email', 'auth.forgot_password', 'auth.reset_password', 'health', 'privacy_notice', 'support_page', 'static'}
        if not public and not g.user:
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Please log in.', 'login_url': '/login'}), 401
            return redirect(url_for('auth.login'))
        if g.user and not g.user['email_verified'] and not public and request.endpoint not in {'auth.logout', 'auth.google_link','remove_account','download_account_data'}:
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Verify your email before using the workspace.'}), 403
            return redirect(url_for('auth.verification_pending'))
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            expected = session.get('csrf_token', '')
            if not supplied or not expected or not hmac.compare_digest(supplied, expected):
                if request.path.startswith('/api/'):
                    return jsonify({'success': False, 'error': 'Session expired. Refresh and try again.'}), 400
                flash('Session expired. Refresh the page and try again.', 'error')
                return redirect(url_for('auth.login'))

    @app.teardown_request
    def unlock_account(error):
        lock=getattr(g,'account_lock',None)
        if lock:
            fcntl.flock(lock,fcntl.LOCK_UN);lock.close();g.account_lock=None

    @app.after_request
    def auth_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if os.environ.get('SYNAPTIFORGE_ENV') == 'production':
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.cli.command('retry-welcome-emails')
    def retry_welcome_emails():
        if not app.config['SMTP_USERNAME'] or not app.config['SMTP_PASSWORD']:
            raise click.ClickException('Configure SMTP_USERNAME and SMTP_PASSWORD first.')
        with database() as db:
            users = db.execute("SELECT user_id FROM mail_outbox WHERE status='pending' OR (status='sending' AND updated < ?)", (int(time.time()) - 600,)).fetchall()
        count = sum(welcome_email(row['user_id']) for row in users)
        click.echo(f'{count} welcome emails accepted by SMTP; unsuccessful messages remain queued.')
