from app.cloud_platforms import resolve_cloud_platforms


def test_none_and_blank_resolve_to_none():
    assert resolve_cloud_platforms(None) is None
    assert resolve_cloud_platforms("") is None


def test_no_mention_resolves_to_none():
    assert resolve_cloud_platforms("We build great software for enterprises.") is None


def test_detects_aws():
    assert resolve_cloud_platforms("Experience with AWS required.") == "AWS"
    assert resolve_cloud_platforms("Experience with Amazon Web Services required.") == "AWS"


def test_detects_gcp():
    assert resolve_cloud_platforms("Experience with GCP required.") == "GCP"
    assert resolve_cloud_platforms("Experience with Google Cloud Platform required.") == "GCP"
    assert resolve_cloud_platforms("Experience with Google Cloud required.") == "GCP"


def test_detects_azure():
    assert resolve_cloud_platforms("Experience with Azure required.") == "Azure"
    assert resolve_cloud_platforms("Experience with Microsoft Azure required.") == "Azure"


def test_detects_multiple_in_fixed_order():
    text = "Must know Azure and GCP; AWS is a plus."
    assert resolve_cloud_platforms(text) == "AWS, GCP, Azure"


def test_detects_all_three():
    text = "Multi-cloud experience across AWS, Azure, and Google Cloud Platform."
    assert resolve_cloud_platforms(text) == "AWS, GCP, Azure"


def test_word_boundary_does_not_false_positive():
    # "awsome" / "awesome" should not trigger AWS
    assert resolve_cloud_platforms("This is an awesome opportunity.") is None
