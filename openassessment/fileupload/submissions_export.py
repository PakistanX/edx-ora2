"""
Bulk export of an ORA's learner submissions (uploaded files and text answers) as a single zip file.

Exports are generated in the background (see `openassessment.fileupload.tasks`) and kept in django's
default storage, next to a small status file which the staff area polls to show the export's progress.
"""
from __future__ import absolute_import

import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
import zipfile
from datetime import timedelta

from django.apps import apps
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import strip_tags
import six

try:
    from html import unescape
except ImportError:  # Python 2
    from six.moves.html_parser import HTMLParser  # pylint: disable=import-error
    unescape = HTMLParser().unescape

from openassessment.fileupload import api as file_upload_api

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

STORAGE_ROOT = 'ora2_submissions_export'

STATUS_NONE = 'none'
STATUS_PROCESSING = 'processing'
STATUS_READY = 'ready'
STATUS_FAILED = 'failed'

# An export still "processing" after this long is assumed to have died (e.g. worker restart).
PROCESSING_TIMEOUT = timedelta(hours=2)

# Zips are buffered in memory up to this size, then spill to disk.
ZIP_MAX_MEMORY_SIZE = 50 * 1024 * 1024

USERNAME_LOOKUP_BATCH_SIZE = 1000


def safe_filename(name):
    """
    Replace characters that are unsafe in file names (path separators, control and reserved characters).
    """
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', name or '').strip(' .')


def get_export_status(course_id, item_id):
    """
    Return the status dict of the latest export for an ORA; `{'status': 'none'}` if there is none.
    """
    path = _status_path(course_id, item_id)
    try:
        if not default_storage.exists(path):
            return {'status': STATUS_NONE}
        with default_storage.open(path, 'rb') as status_file:
            status = json.loads(status_file.read().decode('utf-8'))
    except (IOError, OSError, ValueError):
        logger.exception(u"Could not read ORA submissions export status at %s", path)
        return {'status': STATUS_NONE}

    if status.get('status') == STATUS_PROCESSING:
        requested_at = parse_datetime(status.get('requested_at') or '')
        if requested_at is None or timezone.now() - requested_at > PROCESSING_TIMEOUT:
            status['status'] = STATUS_FAILED
            status['error'] = 'timeout'
    return status


def start_export(course_id, item_id, requested_by=None):
    """
    Mark a new export of an ORA as processing, discarding any previous export.

    Returns:
        str: id of the new export, to be passed on to `build_export`.
    """
    previous = get_export_status(course_id, item_id)
    _delete_file(previous.get('path'))

    export_id = uuid.uuid4().hex
    _write_status(course_id, item_id, {
        'status': STATUS_PROCESSING,
        'export_id': export_id,
        'requested_at': timezone.now().isoformat(),
        'requested_by': requested_by,
    })
    return export_id


def build_export(export_id, course_id, item_id, item_type, ora_name, prompts):
    """
    Generate the zip for export `export_id` and record it as ready (or failed).

    The zip is named `<course_id>_<ora_name>.zip` and holds, for each learner's latest
    non-cancelled submission, `<username>_<file name>` for every uploaded file and
    `<username>_response.txt` with their text answers.
    """
    if get_export_status(course_id, item_id).get('export_id') != export_id:
        logger.info(u"ORA submissions export %s was superseded before it started", export_id)
        return

    try:
        zip_name = u'{}_{}.zip'.format(safe_filename(six.text_type(course_id)), safe_filename(ora_name) or 'ora')
        path = u'{}/{}/{}'.format(_export_dir(course_id, item_id), export_id, zip_name)
        counts = _build_zip(path, course_id, item_id, item_type, prompts)
    except Exception:  # pylint: disable=broad-except
        logger.exception(u"ORA submissions export %s for %s failed", export_id, item_id)
        status = get_export_status(course_id, item_id)
        if status.get('export_id') == export_id:
            status.update({'status': STATUS_FAILED, 'error': 'error'})
            _write_status(course_id, item_id, status)
        return

    status = get_export_status(course_id, item_id)
    if status.get('export_id') != export_id:
        # A newer export was requested while this one was running; it wins.
        _delete_file(path)
        return

    status.update(counts)
    status.update({
        'status': STATUS_READY,
        'path': path,
        'file_name': zip_name,
        'completed_at': timezone.now().isoformat(),
    })
    _write_status(course_id, item_id, status)


def open_export_file(status):
    """
    Return (file, size) for a ready export, or (None, 0) if its zip is no longer in storage.
    """
    path = status.get('path')
    if status.get('status') != STATUS_READY or not path or not default_storage.exists(path):
        return None, 0
    return default_storage.open(path, 'rb'), default_storage.size(path)


def has_newer_submissions(status, course_id, item_id):
    """
    Whether any submission was made to the ORA after a ready export was generated.
    """
    completed_at = parse_datetime(status.get('completed_at') or '')
    if status.get('status') != STATUS_READY or completed_at is None:
        return False
    # Import is placed here to avoid model import at project startup.
    from submissions.models import Submission
    return Submission.objects.filter(
        student_item__course_id=course_id,
        student_item__item_id=item_id,
        created_at__gt=completed_at,
    ).exists()


def _build_zip(path, course_id, item_id, item_type, prompts):
    """
    Write the export zip to `path` in default storage. Returns counts of what it holds.
    """
    # Imports are placed here to avoid model import at project startup.
    from submissions import api as sub_api
    from openassessment.workflow.models import AssessmentWorkflow

    cancelled_submission_uuids = set(
        AssessmentWorkflow.objects.filter(
            course_id=course_id, item_id=item_id, status=AssessmentWorkflow.STATUS.cancelled
        ).values_list('submission_uuid', flat=True)
    )
    submissions = [
        submission
        for submission in sub_api.get_all_submissions(course_id, item_id, item_type, read_replica=False)
        if submission['uuid'] not in cancelled_submission_uuids
    ]
    usernames = _get_usernames([submission['student_id'] for submission in submissions])
    prompt_texts = [_html_to_text(prompt) for prompt in prompts or []]

    used_names = set()
    missing_files = []
    file_count = 0
    with tempfile.SpooledTemporaryFile(max_size=ZIP_MAX_MEMORY_SIZE) as zip_buffer:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for submission in submissions:
                username = safe_filename(usernames.get(submission['student_id']) or submission['student_id'])
                answer = submission.get('answer')
                if not isinstance(answer, dict):
                    answer = {'text': answer} if isinstance(answer, six.string_types) else {}

                response_text = _format_response_text(username, submission, answer, prompt_texts)
                if response_text:
                    zip_file.writestr(
                        _unique_archive_name(u'{}_response.txt'.format(username), used_names),
                        response_text.encode('utf-8'),
                    )

                for key, file_name in _get_file_keys_and_names(answer):
                    archive_name = _unique_archive_name(
                        u'{}_{}'.format(username, safe_filename(file_name) or 'file'), used_names
                    )
                    try:
                        content = file_upload_api.get_file_content(key)
                    except Exception:  # pylint: disable=broad-except
                        logger.exception(u"Could not read ORA file with key %s for submissions export", key)
                        content = None
                    if content is None:
                        missing_files.append(archive_name)
                        continue
                    zip_file.writestr(archive_name, content)
                    file_count += 1

            if missing_files:
                zip_file.writestr(
                    'missing_files.txt',
                    u'The following files could not be retrieved from storage:\n\n{}\n'.format(
                        u'\n'.join(missing_files)
                    ).encode('utf-8'),
                )

        zip_buffer.seek(0)
        default_storage.save(path, File(zip_buffer))

    return {
        'learner_count': len(submissions),
        'file_count': file_count,
        'missing_file_count': len(missing_files),
    }


def _format_response_text(username, submission, answer, prompt_texts):
    """
    Plain-text rendering of a submission's text answers, or '' if it has none.
    """
    parts = answer.get('parts')
    if not isinstance(parts, list):
        parts = [{'text': answer.get('text')}]
    texts = [
        (part.get('text') or '').strip() if isinstance(part, dict) else ''
        for part in parts
    ]
    if not any(texts):
        return ''

    lines = [
        u'Learner: {}'.format(username),
        u'Submitted at: {}'.format(submission.get('submitted_at') or ''),
    ]
    for index, text in enumerate(texts):
        prompt = prompt_texts[index] if index < len(prompt_texts) else ''
        lines.extend([
            u'',
            u'=== Prompt {} ==='.format(index + 1) if len(texts) > 1 else u'=== Prompt ===',
            prompt,
            u'',
            u'--- Response ---',
            text,
        ])
    return u'\n'.join(lines) + u'\n'


def _get_file_keys_and_names(answer):
    """
    Yield (file_key, file_name) pairs for the files attached to a submission's answer.
    Falls back to `file_<n>` when the original name was not stored.
    """
    file_keys = answer.get('file_keys') or ([answer['file_key']] if answer.get('file_key') else [])
    file_names = answer.get('files_names', answer.get('files_name')) or []
    for index, key in enumerate(file_keys):
        if not key:
            continue
        file_name = file_names[index] if index < len(file_names) else ''
        if not isinstance(file_name, six.string_types) or not file_name.strip():
            file_name = u'file_{}'.format(index + 1)
        yield key, file_name.strip()


def _unique_archive_name(name, used_names):
    """
    Return `name`, suffixed with a counter if it is already used in the archive.
    """
    base, ext = os.path.splitext(name)
    candidate, counter = name, 1
    while candidate in used_names:
        counter += 1
        candidate = u'{}_{}{}'.format(base, counter, ext)
    used_names.add(candidate)
    return candidate


def _get_usernames(anonymous_user_ids):
    """
    Map anonymous user ids to usernames. Empty when running outside the LMS.
    """
    try:
        anonymous_user_id_model = apps.get_model('student', 'AnonymousUserId')
    except LookupError:
        return {}
    usernames = {}
    anonymous_user_ids = list(set(anonymous_user_ids))
    for start in range(0, len(anonymous_user_ids), USERNAME_LOOKUP_BATCH_SIZE):
        usernames.update(
            anonymous_user_id_model.objects.filter(
                anonymous_user_id__in=anonymous_user_ids[start:start + USERNAME_LOOKUP_BATCH_SIZE]
            ).values_list('anonymous_user_id', 'user__username')
        )
    return usernames


def _html_to_text(html):
    """
    Plain text of an (HTML) prompt.
    """
    return unescape(strip_tags(html or '')).strip()


def _export_dir(course_id, item_id):
    digest = hashlib.sha1(u'{}|{}'.format(course_id, item_id).encode('utf-8')).hexdigest()
    return u'{}/{}'.format(STORAGE_ROOT, digest)


def _status_path(course_id, item_id):
    return u'{}/status.json'.format(_export_dir(course_id, item_id))


def _write_status(course_id, item_id, status):
    path = _status_path(course_id, item_id)
    # Delete first: some storages pick a new name instead of overwriting an existing file.
    _delete_file(path)
    default_storage.save(path, ContentFile(json.dumps(status).encode('utf-8')))


def _delete_file(path):
    try:
        if path and default_storage.exists(path):
            default_storage.delete(path)
    except (IOError, OSError):
        logger.exception(u"Could not delete %s", path)
