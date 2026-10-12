"""Trusted account-scoped storage; web requests never use legacy shared paths."""
from pathlib import Path
import json
import os
import shutil
import tempfile


def owner_id(value=None):
    if value is None:
        from flask import g, has_request_context
        if has_request_context():
            if not getattr(g, 'user', None):
                raise PermissionError('Login required.')
            value = g.user['id']
    if value is None:
        return None  # Standalone legacy CLI scripts remain compatible.
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise ValueError('Invalid account ID.')
    return int(value)


def data_root(owner=None):
    owner = owner_id(owner)
    base=Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or '.')
    return base / 'data/users' / str(owner) if owner is not None else base / 'data'


def model_root(owner=None):
    owner = owner_id(owner)
    base=Path(os.environ.get('SYNAPTIFORGE_STORAGE_DIR') or '.')
    return base / 'models/users' / str(owner) if owner is not None else base / 'models'


def valid_job_id(job_id):
    if not isinstance(job_id, str) or len(job_id) != 8 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('Invalid job ID.')
    return job_id


def jobs_directory(owner=None):
    return data_root(owner) / 'jobs'


def job_status_path(job_id, owner=None):
    return jobs_directory(owner) / (valid_job_id(job_id) + '.json')


def upload_path(job_id, owner=None):
    return data_root(owner) / 'uploads' / ('training_' + valid_job_id(job_id) + '.csv')


def checkpoint_path(job_id, owner=None):
    return model_root(owner) / 'checkpoints' / ('custom_' + valid_job_id(job_id) + '.joblib')


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate_legacy_workspace(root, original_owner):
    """Copy historical artifacts to the original account; never delete originals."""
    root = Path(root)
    marker = root / 'data/private_workspace_migration.json'
    if marker.exists():
        return json.loads(marker.read_text())
    original_owner = owner_id(original_owner)
    relative_data = data_root(original_owner)
    relative_models = model_root(original_owner)
    for source, target in ((root/'models/jobs', root/relative_models/'jobs'),
                           (root/'models/checkpoints', root/relative_models/'checkpoints')):
        if source.exists():
            shutil.copytree(source, target, dirs_exist_ok=True)
            private_directory(target)
    legacy_model = root/'models/custom_distributed_model.joblib'
    if legacy_model.exists():
        private_directory(root/relative_models)
        shutil.copy2(legacy_model, root/relative_models/'custom_distributed_model.joblib')
    count = 0
    for source in (root/'data/jobs').glob('*.json'):
        try:
            job_id = valid_job_id(source.stem)
            status = json.loads(source.read_text())
            if not isinstance(status, dict):
                continue
        except (ValueError, OSError):
            continue
        status['owner_id'] = original_owner
        csv = root/'data'/f'training_{job_id}.csv'
        target_csv = upload_path(job_id, original_owner)
        if csv.exists():
            private_directory((root/target_csv).parent)
            shutil.copy2(csv, root/target_csv)
        status['csv_path'] = str(target_csv)
        if status.get('checkpoint_path'):
            status['checkpoint_path'] = str(checkpoint_path(job_id, original_owner))
        if (root/relative_models/'jobs'/f'{job_id}.joblib').exists():
            status['model_path'] = str(relative_models/'jobs'/f'{job_id}.joblib')
        write_json(root/job_status_path(job_id, original_owner), status)
        count += 1
    active = root/'models/active_custom_model.json'
    if active.exists():
        selected = json.loads(active.read_text())
        valid_job_id(selected['job_id'])
        write_json(root/relative_models/'active_custom_model.json', selected)
    result = {'original_owner_id': original_owner, 'copied_jobs': count}
    write_json(marker, result)
    return result
