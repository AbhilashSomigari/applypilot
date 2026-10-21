from applypilot.models import ATS
from applypilot.sources.ashby import Ashby
from applypilot.sources.greenhouse import Greenhouse
from applypilot.sources.lever import Lever

SOURCES = {ATS.GREENHOUSE: Greenhouse(), ATS.LEVER: Lever(), ATS.ASHBY: Ashby()}

__all__ = ["SOURCES"]
