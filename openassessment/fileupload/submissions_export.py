"""
ilmX: per-ORA bulk export of learner submissions (uploaded files and text answers) as a single zip file,
for the staff "Download All Submissions" button.

The zip itself is built by ``openassessment.data.OraDownloadData`` (shared with the course-wide
"ORA submission files" instructor report); this module only tracks the export's status.

Exports are generated in the background (see `openassessment.fileupload.tasks`) and kept in django's
default storage, next to a small status file which the staff area polls to show the export's progress.
"""
import hashlib
import json
import logging
import tempfile
import uuid
from datetime import timedelta

from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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


def safe_filename(name):
    """
    Replace characters that are unsafe in file names (path separators, control and reserved characters).
    """
    from openassessment.data import _safe_filename  # pylint: disable=import-outside-toplevel
    return _safe_filename(name)


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
        logger.exception("Could not read ORA submissions export status at %s", path)
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


def build_export(  # pylint: disable=too-many-positional-arguments
    export_id, course_id, item_id, item_type, ora_name, prompts
):
    """
    Generate the zip for export `export_id` and record it as ready (or failed).

    The zip is named `<course_id>_<ora_name>.zip` and holds, for each learner's latest
    non-cancelled submission, `<username>_<file name>` for every uploaded file and
    `<username>_response.txt` with their text answers.
    """
    if get_export_status(course_id, item_id).get('export_id') != export_id:
        logger.info("ORA submissions export %s was superseded before it started", export_id)
        return

    try:
        zip_name = '{}_{}.zip'.format(safe_filename(str(course_id)), safe_filename(ora_name) or 'ora')
        path = '{}/{}/{}'.format(_export_dir(course_id, item_id), export_id, zip_name)
        counts = _build_zip(path, course_id, item_id, item_type, prompts)
    except Exception:  # pylint: disable=broad-except
        logger.exception("ORA submissions export %s for %s failed", export_id, item_id)
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
    Write the export zip to `path` in default storage, using ``OraDownloadData`` (the same
    attachment download + zip code as the course-wide "ORA submission files" instructor report).
    Returns counts of what it holds.
    """
    # Imports are placed here to avoid model import at project startup.
    from openassessment.data import OraDownloadData
    from openassessment.workflow.models import AssessmentWorkflow

    cancelled_submission_uuids = AssessmentWorkflow.objects.filter(
        course_id=course_id, item_id=item_id, status=AssessmentWorkflow.STATUS.cancelled
    ).values_list('submission_uuid', flat=True)
    files_data = OraDownloadData.collect_ora2_block_submission_files(
        course_id, item_id, item_type, prompts, excluded_submission_uuids=cancelled_submission_uuids,
    )

    with tempfile.SpooledTemporaryFile(max_size=ZIP_MAX_MEMORY_SIZE) as zip_buffer:
        counts = OraDownloadData.create_zip_with_attachments(
            zip_buffer, files_data, missing_files_name='missing_files.txt'
        )
        zip_buffer.seek(0)
        default_storage.save(path, File(zip_buffer))

    return {
        'learner_count': len({file_data['student_id'] for file_data in files_data}),
        'file_count': counts['file_count'],
        'missing_file_count': counts['missing_file_count'],
    }


def _export_dir(course_id, item_id):
    digest = hashlib.sha1('{}|{}'.format(course_id, item_id).encode('utf-8')).hexdigest()
    return '{}/{}'.format(STORAGE_ROOT, digest)


def _status_path(course_id, item_id):
    return '{}/status.json'.format(_export_dir(course_id, item_id))


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
        logger.exception("Could not delete %s", path)
