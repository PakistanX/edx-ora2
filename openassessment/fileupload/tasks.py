"""
Celery tasks for ORA file uploads.
"""
from __future__ import absolute_import

from celery import shared_task

from openassessment.fileupload import submissions_export


@shared_task(name='openassessment.fileupload.tasks.generate_submissions_export')
def generate_submissions_export(export_id, course_id, item_id, item_type, ora_name, prompts):
    """
    Build the zip of all learner submissions for an ORA (see `submissions_export.build_export`).
    """
    submissions_export.build_export(export_id, course_id, item_id, item_type, ora_name, prompts)
