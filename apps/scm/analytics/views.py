# Analytics views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.shortcuts import render

from apps.teams.decorators import login_and_team_required
