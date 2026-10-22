import shutil
from pathlib import Path

import pytest

from applypilot.apply import filler
from applypilot.apply.answers import Answer
from applypilot.apply.forms import Field

CHROME = Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome")
pytestmark = [pytest.mark.browser, pytest.mark.skipif(not CHROME, reason="needs a local Chrome")]


@pytest.fixture(autouse=True)
def _own_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_HOME", str(tmp_path))  # never touch the real ApplyPilot Chrome profile

LEVER = """<html><body><form class="application-form">
<input type="text" name="name"><input type="email" name="email">
<select name="cards[c1][field0]"><option>Select ...</option><option>Yes</option><option>No</option></select>
<input type="radio" name="cards[c1][field1]" value="Python"><input type="radio" name="cards[c1][field1]" value="Java">
<button type="submit" onclick="document.title='SUBMITTED'">Submit</button>
</form></body></html>"""

ASHBY = """<html><body><form>
<label for="_systemfield_name">Full Name</label><input id="_systemfield_name" name="_systemfield_name">
<div><label>Degree</label>
<input type="radio" id="f_2ea5-labeled-radio-0" name="f_2ea5">
<label for="f_2ea5-labeled-radio-0">Bachelor's Degree</label>
<input type="radio" id="f_2ea5-labeled-radio-1" name="f_2ea5">
<label for="f_2ea5-labeled-radio-1">Master's Degree</label>
</div>
<div><label>Location</label><input id="loc" placeholder="Start typing..."></div>
</form></body></html>"""


def A(key, label, kind, value, options=()):
    return Answer(Field(key, label, kind, True, list(options)), value, "profile")


async def test_lever_style_fill_and_readback_never_submits(tmp_path):
    page = tmp_path / "lever.html"
    page.write_text(LEVER)
    plan = [A("name", "Full name", "text", "Jane Doe"), A("email", "Email", "email", "j@example.com"),
            A("cards[c1][field0]", "Sponsorship?", "select", "Yes", ["Yes", "No"]),
            A("cards[c1][field1]", "Language", "select", "Python", ["Python", "Java"])]
    res, shot = await filler.fill("lever", page.as_uri(), plan, headless=True, out_dir=tmp_path)
    assert [r.status for r in res] == ["filled"] * 4 and shot.exists()
    assert res[2].got == "Yes" and res[3].got == "Python"


async def test_ashby_style_radio_labels_and_plain_text_location(tmp_path):
    page = tmp_path / "ashby.html"
    page.write_text(ASHBY)
    plan = [A("_systemfield_name", "Full Name", "text", "Jane Doe"),
            A("2ea5", "Degree", "select", "Master's Degree", ["Bachelor's Degree", "Master's Degree"]),
            A("_systemfield_location", "Location", "location", "Pittsburgh, PA")]
    res, _ = await filler.fill("ashby", page.as_uri(), plan, headless=True, out_dir=tmp_path)
    assert [(r.status, r.got) for r in res] == [("filled", "Jane Doe"), ("filled", "Master's Degree"),
                                                ("filled", "Pittsburgh, PA")]


async def test_a_mismatch_is_reported_not_hidden(tmp_path):
    page = tmp_path / "lever.html"
    page.write_text(LEVER)
    res, _ = await filler.fill("lever", page.as_uri(), [A("cards[c1][field0]", "Sponsorship?", "select", "Maybe",
                                                          ["Yes", "No"])], headless=True, out_dir=tmp_path)
    assert res[0].status == "failed"
