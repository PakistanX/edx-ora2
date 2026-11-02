"""
ilmX: tests for the ilmX additions to openassessment.data.
"""
import io
import zipfile
from unittest import mock

from django.test import TestCase, override_settings

from submissions import api as sub_api

from openassessment.data import FileMissingException, OraAggregateData, OraDownloadData

COURSE_ID = 'course-v1:Org+CS101+2025'
ITEM_ID = 'block-v1:Org+CS101+2025+type@openassessment+block@essay'
STUDENT_ITEM = {'course_id': COURSE_ID, 'item_id': ITEM_ID, 'item_type': 'openassessment'}


@mock.patch('openassessment.data.OraAggregateData._map_block_usage_keys_to_display_names', mock.Mock(return_value={}))
class ReportEmailTest(TestCase):
    """
    The "Real Student Email" column added after "Anonymized Student ID".
    """

    def setUp(self):
        super().setUp()
        for student_id in ('alice-anon', 'bob-anon'):
            sub_api.create_submission(dict(STUDENT_ITEM, student_id=student_id), {'parts': [{'text': 'hi'}]})

    def _collect(self):
        user_data = {'alice-anon': {'email': 'alice@example.com', 'username': 'alice', 'fullname': 'Alice'}}
        with mock.patch('openassessment.data.map_anonymized_ids_to_user_data', return_value=user_data) as map_mock:
            header, rows = OraAggregateData.collect_ora2_data(COURSE_ID)
        return header, rows, map_mock

    def test_email_column(self):
        header, rows, map_mock = self._collect()
        index = header.index('Real Student Email')
        self.assertEqual(header[index - 1], 'Anonymized Student ID')
        self.assertEqual(header[index + 1], 'Date/Time Response Submitted')
        emails = {row[index - 1]: row[index] for row in rows}
        # Learners without a known email fall back to their anonymized id, as on Juniper.
        self.assertEqual(emails, {'alice-anon': 'alice@example.com', 'bob-anon': 'bob-anon'})
        # One batched lookup for every learner.
        map_mock.assert_called_once_with({'alice-anon', 'bob-anon'})
        self.assertTrue(all(len(row) == len(header) for row in rows))

    @override_settings(ILMX_ORA_REPORT_INCLUDE_EMAIL=False)
    def test_email_column_disabled(self):
        header, rows, map_mock = self._collect()
        self.assertNotIn('Real Student Email', header)
        map_mock.assert_not_called()
        self.assertTrue(all(len(row) == len(header) for row in rows))


class BlockSubmissionFilesTest(TestCase):
    """
    OraDownloadData.collect_ora2_block_submission_files and the missing_files.txt option.
    """

    def test_current_answer_format(self):
        sub_api.create_submission(dict(STUDENT_ITEM, student_id='bob-anon'), {
            'parts': [{'text': '<b>answer</b>'}],
            'file_keys': ['k1', 'k2'],
            'files_descriptions': ['first', 'second'],
            'files_names': ['report.pdf', 'report.pdf'],
            'files_sizes': [3, 4],
        })
        # Another ORA in the course is not included.
        sub_api.create_submission(
            dict(STUDENT_ITEM, student_id='bob-anon', item_id=ITEM_ID + '2'), {'parts': [{'text': 'other'}]}
        )
        with mock.patch('openassessment.data.map_anonymized_ids_to_usernames', return_value={'bob-anon': 'bob'}):
            files = OraDownloadData.collect_ora2_block_submission_files(
                COURSE_ID, ITEM_ID, 'openassessment', ['<p>Prompt one</p>']
            )
        self.assertEqual(
            [(f['type'], f['file_path'], f['description']) for f in files],
            [
                ('attachment', 'bob_report.pdf', 'first'),
                ('attachment', 'bob_report_2.pdf', 'second'),
                ('text', 'bob_response.txt', 'Submission text.'),
            ],
        )
        self.assertIn('=== Prompt ===\nPrompt one\n\n--- Response ---\n<b>answer</b>', files[2]['content'])

        def download(key):
            if key == 'k2':
                raise FileMissingException
            return b'content'

        buf = io.BytesIO()
        with mock.patch('openassessment.data.OraDownloadData._download_file_by_key', side_effect=download):
            counts = OraDownloadData.create_zip_with_attachments(buf, files, missing_files_name='missing_files.txt')
        self.assertEqual(counts, {'file_count': 1, 'missing_file_count': 1})
        with zipfile.ZipFile(buf) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ['bob_report.pdf', 'bob_response.txt', 'missing_files.txt', 'submissions.csv'],
            )
            self.assertIn(b'bob_report_2.pdf', archive.read('missing_files.txt'))

    def test_course_wide_zip_unchanged_without_missing_files_name(self):
        buf = io.BytesIO()
        files = [{
            'type': 'attachment', 'course_id': COURSE_ID, 'block_id': ITEM_ID, 'student_id': 's',
            'key': 'k', 'name': 'n', 'description': '', 'size': 0, 'file_path': 'a/n',
        }]
        with mock.patch('openassessment.data.OraDownloadData._download_file_by_key', side_effect=FileMissingException):
            self.assertTrue(OraDownloadData.create_zip_with_attachments(buf, files))
        with zipfile.ZipFile(buf) as archive:
            self.assertEqual(archive.namelist(), ['submissions.csv'])
