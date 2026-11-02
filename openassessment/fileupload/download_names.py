"""
ilmX: download links that keep the learner's original file name and type.

Files are stored under their upload key (no name, no extension), so a plain signed link downloads
as a nameless file. Backends that sign links (S3, and django storage on S3) add these response
headers when the caller knows the file name.
"""
import mimetypes
import os
from urllib.parse import quote

# Extensions mimetypes may not know; ORA "custom" uploads include Power BI files.
EXTRA_CONTENT_TYPES = {
    '.pbix': 'application/x-pbix',
}


def content_type(file_name):
    """Content-Type for `file_name`, application/octet-stream when unknown."""
    extension = os.path.splitext(file_name)[1].lower()
    if extension in EXTRA_CONTENT_TYPES:
        return EXTRA_CONTENT_TYPES[extension]
    return mimetypes.guess_type(file_name)[0] or 'application/octet-stream'


def content_disposition(file_name):
    """attachment; filename=<ASCII fallback>; filename*=<UTF-8 name> (RFC 6266), safe for Urdu names."""
    fallback = file_name.encode('ascii', 'replace').decode('ascii').replace('?', '_')
    fallback = fallback.replace('\\', '_').replace('"', '_').replace('\r', '').replace('\n', '')
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(file_name, safe="")}'


def response_headers(file_name):
    """(Content-Disposition, Content-Type) for a named download, or None without a usable name."""
    file_name = os.path.basename((file_name or '').strip())
    if not file_name:
        return None
    return content_disposition(file_name), content_type(file_name)
