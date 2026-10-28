"""Orders for an install without a broker: there is nowhere live to send them.

The install is held in Sandbox mode (database/settings_db.py), so orders,
books and positions are served by the sandbox engine and never reach this
module. Anything that does reach it is refused with the reason, rather than
answered with an empty book that would read as "you hold nothing".
"""

from utils.data_mode import LIVE_UNAVAILABLE_MESSAGE


class NoBrokerConnected(Exception):
    def __init__(self):
        super().__init__(LIVE_UNAVAILABLE_MESSAGE)


def _refuse(*args, **kwargs):
    raise NoBrokerConnected()


get_order_book = _refuse
get_trade_book = _refuse
get_positions = _refuse
get_holdings = _refuse
get_open_position = _refuse
place_order_api = _refuse
place_smartorder_api = _refuse
close_all_positions = _refuse
cancel_order = _refuse
modify_order = _refuse
cancel_all_orders_api = _refuse
