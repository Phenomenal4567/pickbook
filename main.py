"""Compatibility entrypoint for hosts configured as ``main:app``.

The live application is defined in ``app.main`` so all deployments share the
same author-only router set.
"""

from app.main import app

