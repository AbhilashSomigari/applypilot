import pytest

from applypilot.models import ATS, Job
from applypilot.relevance import judge


def job(title, location="New York, NY", country=None):
    return Job(ATS.GREENHOUSE, "x", "1", title, "X", location, country)


@pytest.mark.parametrize(
    "title,loc,relevant,new_grad",
    [
        ("Software Engineer, New Grad (2026)", "San Francisco, CA", True, True),
        ("Machine Learning Engineer", "Remote", True, False),
        ("Software Engineer I", "Pittsburgh, PA", True, True),
        ("AI Engineer - Early Career", "Remote - US", True, True),
        ("Data Scientist", "Charlotte, NC", True, False),
        ("Forward Deployed Engineer", "New York City", True, False),
        ("Senior Software Engineer", "Seattle, WA", False, False),
        ("Staff ML Engineer", "Remote", False, False),
        ("Engineering Manager, Platform", "Remote", False, False),
        ("Software Engineer III", "Austin, TX", False, False),
        ("Software Engineering Intern, Summer 2027", "Boston, MA", False, False),
        ("Mechanical Engineer", "Detroit, MI", False, False),
        ("Cleared Software Engineer", "Charlottesville, VA", False, False),
        ("Software Engineer (TS/SCI with Polygraph)", "Fort Meade, MD", False, False),
        ("Account Executive", "New York", False, False),
        ("Specialist AI GTM Strategy and Activation", "Chicago, IL", False, False),
        ("Software Engineer", "London, United Kingdom", False, False),
        ("Software Engineer", "Bengaluru", False, False),
        ("Associate Data Platform Engineer", "Cape Town", False, False),
        ("Software Engineer", "Toronto, Canada | New York, NY", True, False),
        ("Associate Software Engineer", "Jersey City, NJ", True, True),
    ],
)
def test_judge(title, loc, relevant, new_grad):
    v = judge(job(title, loc))
    assert v.relevant is relevant, v.reason
    if relevant:
        assert v.new_grad is new_grad


def test_country_field_wins_over_text():
    assert not judge(job("Software Engineer", "Singapore", "SG")).relevant
    assert judge(job("Software Engineer", "Washington, D.C.", "US")).relevant


def test_unknown_location_is_kept():
    v = judge(job("Backend Engineer", ""))
    assert v.relevant and "unknown" in v.reason
