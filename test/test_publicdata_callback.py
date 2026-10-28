"""The publicdata plugin must never complete a broker login through the callback.

``/<broker>/callback`` calls whichever plugin's ``authenticate_broker`` matches
the URL, for any logged-in session. The publicdata plugin exists on every
install, so if its auth function returned a token, a link to
``/publicdata/callback?code=x`` opened in a logged-in browser would replace the
stored broker session and tear the live feed down.
"""

from broker.publicdata.api import auth_api


def test_authenticate_broker_refuses():
    token, error = auth_api.authenticate_broker("anything")
    assert token is None
    assert "broker" in error.lower()


def test_the_callback_route_does_not_log_in(monkeypatch):
    """Drive the real callback with the real publicdata auth function."""
    import inspect

    from flask import Flask, session

    import blueprints.brlogin as brlogin

    calls = []
    monkeypatch.setattr(brlogin, "handle_auth_success", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(brlogin, "handle_auth_failure", lambda *a, **k: "refused")

    app = Flask(__name__)
    app.secret_key = "test-only"
    app.broker_auth_functions = {"publicdata_auth": auth_api.authenticate_broker}
    callback = inspect.unwrap(brlogin.broker_callback)

    with app.test_request_context("/publicdata/callback?code=x"):
        session["user"] = "devtrader"
        response = callback("publicdata")

    assert calls == [], "the publicdata plugin completed a broker login"
    assert response == "refused"
