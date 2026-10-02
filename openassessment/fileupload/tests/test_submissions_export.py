"""
Tests for the bulk "all submissions" zip export.
"""
from __future__ import absolute_import

from datetime import timedelta
import io
import shutil
import tempfile
import zipfile

from mock import patch

from django.core.files.storage import default_storage
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from freezegun import freeze_time

from openassessment.fileupload import submissions_export
from openassessment.workflow import api as workflow_api
from submissions import api as sub_api

COURSE_ID = 'course-v1:Org+CS101+2025'
ITEM_ID = 'block-v1:Org+CS101+2025+type@openassessment+block@essay'
ITEM_TYPE = 'openassessment'
PROMPTS = [u'<p>Describe your <b>design</b>.</p>', u'Explain the trade-offs &amp; risks.']
USERNAMES = {'bob-anon': 'bob_ilmx', 'alice-anon': 'alice_ilmx', 'carol-anon': 'carol_ilmx'}
FILE_CONTENTS = {'bob_1': b'bob one', 'bob_2': b'bob two', 'alice_1': b'alice', 'carol_1': b'carol'}


class SubmissionsExportTest(TestCase):
    """
    Tests for building, tracking and reading back the submissions export.
    """

    def setUp(self):
        super(SubmissionsExportTest, self).setUp()
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        self._override_media_root(media_root)

        for target, kwargs in (
            ('openassessment.fileupload.submissions_export._get_usernames', {'return_value': USERNAMES}),
            ('openassessment.fileupload.submissions_export.file_upload_api.get_file_content',
             {'side_effect': FILE_CONTENTS.get}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _override_media_root(self, media_root):
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)

    @staticmethod
    def _submit(student_id, answer, cancel=False):
        student_item = {
            'student_id': student_id, 'course_id': COURSE_ID, 'item_id': ITEM_ID, 'item_type': ITEM_TYPE,
        }
        submission = sub_api.create_submission(student_item, answer)
        workflow_api.create_workflow(submission['uuid'], ['self'])
        if cancel:
            workflow_api.cancel_workflow(submission['uuid'], 'cancelled', 'staff', {})
        return submission

    @staticmethod
    def _run_export():
        export_id = submissions_export.start_export(COURSE_ID, ITEM_ID)
        submissions_export.build_export(export_id, COURSE_ID, ITEM_ID, ITEM_TYPE, u'Essay: Week 1', PROMPTS)
        return submissions_export.get_export_status(COURSE_ID, ITEM_ID)

    @staticmethod
    def _read_zip(status):
        export_file, __ = submissions_export.open_export_file(status)
        with export_file:
            return zipfile.ZipFile(io.BytesIO(export_file.read()))

    def test_no_export(self):
        status = submissions_export.get_export_status(COURSE_ID, ITEM_ID)
        self.assertEqual(status, {'status': 'none'})
        self.assertEqual(submissions_export.open_export_file(status), (None, 0))

    def test_export(self):
        # Only Bob's latest attempt is exported.
        self._submit('bob-anon', {'parts': [{'text': u'old'}], 'file_keys': ['bob_old'], 'files_names': ['old.pdf']})
        self._submit('bob-anon', {
            'parts': [{'text': u'Bob design'}, {'text': u'Bob risks'}],
            'file_keys': ['bob_1', 'bob_2', 'bob_missing'],
            'files_names': ['report.pdf', 'report.pdf', 'gone.pdf'],
        })
        # Files only, with no stored file name.
        self._submit('alice-anon', {'parts': [{'text': u''}, {'text': u''}], 'file_keys': ['alice_1']})
        # Text only, from a learner whose username can't be resolved.
        self._submit('dave-anon', {'parts': [{'text': u'Dave design'}, {'text': u''}]})
        # Cancelled submissions are left out.
        self._submit('carol-anon', {'parts': [{'text': u'Carol'}], 'file_keys': ['carol_1']}, cancel=True)

        status = self._run_export()

        self.assertEqual(status['status'], 'ready')
        self.assertEqual(status['file_name'], u'course-v1_Org+CS101+2025_Essay_ Week 1.zip')
        self.assertEqual(
            (status['learner_count'], status['file_count'], status['missing_file_count']), (3, 3, 1)
        )
        archive = self._read_zip(status)
        self.assertEqual(sorted(archive.namelist()), [
            'alice_ilmx_file_1',
            'bob_ilmx_report.pdf',
            'bob_ilmx_report_2.pdf',
            'bob_ilmx_response.txt',
            'dave-anon_response.txt',
            'missing_files.txt',
        ])
        self.assertEqual(archive.read('bob_ilmx_report.pdf'), b'bob one')
        self.assertEqual(archive.read('bob_ilmx_report_2.pdf'), b'bob two')
        self.assertIn(b'bob_ilmx_gone.pdf', archive.read('missing_files.txt'))

        bob_response = archive.read('bob_ilmx_response.txt').decode('utf-8')
        self.assertIn(u'Learner: bob_ilmx', bob_response)
        self.assertIn(u'=== Prompt 1 ===\nDescribe your design.\n\n--- Response ---\nBob design', bob_response)
        self.assertIn(u'=== Prompt 2 ===\nExplain the trade-offs & risks.\n\n--- Response ---\nBob risks', bob_response)
        self.assertNotIn(u'old', bob_response)

    def test_legacy_text_answer(self):
        self._submit('bob-anon', {'text': u'Legacy answer'})
        archive = self._read_zip(self._run_export())
        self.assertIn(u'Legacy answer', archive.read('bob_ilmx_response.txt').decode('utf-8'))

    def test_regenerate_replaces_previous_zip(self):
        self._submit('bob-anon', {'parts': [{'text': u'Bob'}]})
        first = self._run_export()
        self.assertFalse(submissions_export.has_newer_submissions(first, COURSE_ID, ITEM_ID))

        self._submit('alice-anon', {'parts': [{'text': u'Alice'}]})
        self.assertTrue(submissions_export.has_newer_submissions(first, COURSE_ID, ITEM_ID))

        second = self._run_export()
        self.assertFalse(default_storage.exists(first['path']))
        self.assertEqual(second['learner_count'], 2)

    def test_superseded_export_is_discarded(self):
        self._submit('bob-anon', {'parts': [{'text': u'Bob'}]})
        stale_export_id = submissions_export.start_export(COURSE_ID, ITEM_ID)
        current_export_id = submissions_export.start_export(COURSE_ID, ITEM_ID)

        submissions_export.build_export(stale_export_id, COURSE_ID, ITEM_ID, ITEM_TYPE, u'Essay', PROMPTS)
        status = submissions_export.get_export_status(COURSE_ID, ITEM_ID)
        self.assertEqual((status['status'], status['export_id']), ('processing', current_export_id))

    def test_failed_export(self):
        self._submit('bob-anon', {'parts': [{'text': u'Bob'}]})
        with patch('openassessment.fileupload.submissions_export._build_zip', side_effect=Exception('boom')):
            status = self._run_export()
        self.assertEqual((status['status'], status['error']), ('failed', 'error'))

    def test_stale_processing_export_is_failed(self):
        submissions_export.start_export(COURSE_ID, ITEM_ID)
        self.assertEqual(submissions_export.get_export_status(COURSE_ID, ITEM_ID)['status'], 'processing')

        with freeze_time(timezone.now() + submissions_export.PROCESSING_TIMEOUT + timedelta(minutes=1)):
            status = submissions_export.get_export_status(COURSE_ID, ITEM_ID)
        self.assertEqual((status['status'], status['error']), ('failed', 'timeout'))
