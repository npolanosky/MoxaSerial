"""The palette page must be handed to Fusion as a file URI (Windows fix)."""

from moxaserial.ui.palette import palette_html_path, palette_html_url


def test_palette_url_is_a_file_uri():
    url = palette_html_url()
    assert url.startswith("file:///")
    assert "\\" not in url
    assert url.endswith("/resources/palette/index.html")
    assert palette_html_path().endswith("index.html")
