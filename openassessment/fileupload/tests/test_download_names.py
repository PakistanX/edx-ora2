"""
ilmX: download links carry the learner's original file name and content type.
"""
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import boto3
from django.test import TestCase
from django.test.utils import override_settings
from moto import mock_s3

from openassessment.fileupload import api, download_names
from openassessment.fileupload.backends import django_storage


class TestDownloadNames(TestCase):
    """ Header values built from a file name. """

    def test_content_types(self):
        self.assertEqual(download_names.content_type("report.PBIX"), "application/x-pbix")
        self.assertEqual(download_names.content_type("essay.pdf"), "application/pdf")
        self.assertEqual(download_names.content_type("no-extension"), "application/octet-stream")

    def test_disposition_keeps_unicode_and_strips_unsafe_characters(self):
        disposition, content_type = download_names.response_headers('dir/مضمون "final".docx\r\n')
        self.assertEqual(
            disposition,
            'attachment; filename="_____ _final_.docx"; '
            "filename*=UTF-8''%D9%85%D8%B6%D9%85%D9%88%D9%86%20%22final%22.docx",
        )
        self.assertEqual(content_type, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_no_name_no_headers(self):
        self.assertIsNone(download_names.response_headers(None))
        self.assertIsNone(download_names.response_headers("  "))


@override_settings(
    AWS_ACCESS_KEY_ID="foobar",
    AWS_SECRET_ACCESS_KEY="bizbaz",
    FILE_UPLOAD_STORAGE_BUCKET_NAME="mybucket",
)
class TestS3DownloadName(TestCase):
    """ The S3 backend signs the name and type into the link. """

    def _put(self):
        conn = boto3.client("s3")
        conn.create_bucket(Bucket="mybucket")
        conn.put_object(Bucket="mybucket", Key="submissions_attachments/foo", Body=b"data")

    @mock_s3
    def test_named_link(self):
        self._put()
        query = parse_qs(urlparse(api.get_download_url("foo", file_name="Model.pbix")).query)
        self.assertEqual(query["response-content-disposition"],
                         ["attachment; filename=\"Model.pbix\"; filename*=UTF-8''Model.pbix"])
        self.assertEqual(query["response-content-type"], ["application/x-pbix"])

    @mock_s3
    def test_unnamed_link_unchanged(self):
        self._put()
        query = parse_qs(urlparse(api.get_download_url("foo")).query)
        self.assertNotIn("response-content-disposition", query)


@override_settings(ORA2_FILEUPLOAD_BACKEND="django", LMS_ROOT_URL="https://lms.test")
class TestDjangoStorageDownloadName(TestCase):
    """ django storage: signing storages get parameters, others are called as before. """

    def test_signing_storage_gets_parameters(self):
        calls = []

        class SigningStorage:
            def exists(self, _name):
                return True

            def url(self, _name, parameters=None, _expire=None):
                calls.append(parameters)
                return "https://s3/x"

        with patch.object(django_storage, "default_storage", SigningStorage()):
            url = api.get_download_url("foo", file_name="essay.pdf")
        self.assertEqual(url, "https://s3/x")
        self.assertEqual(calls[0]["ResponseContentType"], "application/pdf")
        self.assertTrue(calls[0]["ResponseContentDisposition"].startswith('attachment; filename="essay.pdf"'))

    def test_plain_storage_called_without_parameters(self):
        class PlainStorage:
            def exists(self, _name):
                return True

            def url(self, _name):
                return "/media/x"

        with patch.object(django_storage, "default_storage", PlainStorage()):
            url = api.get_download_url("foo", file_name="essay.pdf")
        self.assertEqual(url, "https://lms.test/media/x")

    def test_unreadable_signature_is_treated_as_plain(self):
        self.assertFalse(django_storage._accepts_parameters(Mock()))  # pylint: disable=protected-access

    def test_backend_without_name_support(self):
        backend = Mock(spec=["get_download_url"])
        backend.get_download_url.return_value = "https://x"
        with patch("openassessment.fileupload.backends.get_backend", return_value=backend):
            api.get_download_url("foo", file_name="essay.pdf")
        backend.get_download_url.assert_called_once_with("foo")
