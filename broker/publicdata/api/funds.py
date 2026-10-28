"""Funds for an install without a broker.

There is no broker account, so there are no broker funds. Sandbox mode keeps
its own capital (sandbox.db), which is what every funds screen shows here.
"""


def test_auth_token(auth_token):
    """The placeholder token never expires: there is no broker session behind it."""
    return True, None


def get_margin_data(auth_token):
    return {
        "availablecash": "0.00",
        "collateral": "0.00",
        "m2munrealized": "0.00",
        "m2mrealized": "0.00",
        "utiliseddebits": "0.00",
    }
