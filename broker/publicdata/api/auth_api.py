"""Login for an install without a broker: there is no broker to log in to.

blueprints/auth.py completes the OpenAlgo login directly in public-data mode
(``_connect_public_data``), which is gated on ``OPENALGO_DATA_MODE``. This
function is never part of that path, and it refuses.

It must refuse. The plugin directory exists on every install, and
``/<broker>/callback`` calls the matching plugin's ``authenticate_broker`` for
any logged-in session. If this returned a token, a link to
``/publicdata/callback?code=x`` opened in a logged-in browser would replace the
stored broker session and tear the live feed down.
"""


def authenticate_broker(code=None):
    """Always refuses. Returns (None, a message for the trader)."""
    return None, "There is no broker login for public market data. Log in with your broker."
