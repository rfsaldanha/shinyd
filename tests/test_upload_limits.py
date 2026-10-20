import pytest
from django.core.files.uploadhandler import StopUpload

from control.uploads import BoundedUploadHandler


def test_streaming_file_budget_counts_across_multiple_files(settings):
    settings.SHINYD_UPLOAD_LIMIT = 10
    handler = BoundedUploadHandler()
    assert handler.receive_data_chunk(b"123456", 0) == b"123456"
    handler.file_complete(6)
    with pytest.raises(StopUpload):
        handler.receive_data_chunk(b"78901", 0)


def test_content_length_rejected_before_form_parsing(client, settings):
    settings.SHINYD_UPLOAD_LIMIT = 10
    response = client.post("/login/", {}, CONTENT_LENGTH=str(3 * 1024 * 1024))
    assert response.status_code == 413
