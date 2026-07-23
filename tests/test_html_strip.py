from app.html_strip import strip_html


def test_strip_html_removes_tags():
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_unescapes_entities():
    assert strip_html("Salary: &gt;$100k &amp; equity") == "Salary: >$100k & equity"


def test_strip_html_handles_none_and_empty():
    assert strip_html(None) == ""
    assert strip_html("") == ""


def test_strip_html_collapses_block_tags_to_newlines():
    result = strip_html("<div>Line one</div><div>Line two</div>")
    assert result == "Line one\n\nLine two"


def test_strip_html_plain_text_passthrough():
    assert strip_html("just plain text") == "just plain text"
