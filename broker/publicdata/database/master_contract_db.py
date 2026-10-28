"""Symbol table for installs without a broker, built from official NSE files.

The latest published cash and F&O bhavcopy give every NSE equity, every live
NFO future and option with its lot size, and the F&O index underlyings. See
services/public_data/nse_symbols.py for how each becomes an OpenAlgo symbol.
"""

import os

from sqlalchemy import Column, Float, Index, Integer, Sequence, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from extensions import socketio
from services.public_data.nse_archive import ArchiveUnavailable, fetch_bhavcopy, latest_trading_day
from services.public_data.nse_symbols import build_symbol_table
from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_db_engine(DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class SymToken(Base):
    __tablename__ = "symtoken"
    id = Column(Integer, Sequence("symtoken_id_seq"), primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    brsymbol = Column(String, nullable=False, index=True)
    name = Column(String)
    exchange = Column(String, index=True)
    brexchange = Column(String, index=True)
    token = Column(String, index=True)
    expiry = Column(String)
    strike = Column(Float)
    lotsize = Column(Integer)
    instrumenttype = Column(String)
    tick_size = Column(Float)

    __table_args__ = (Index("idx_symbol_exchange", "symbol", "exchange"),)


def init_db():
    logger.info("Initializing Master Contract DB")
    Base.metadata.create_all(bind=engine)


def _replace_symbols(rows: list[dict]) -> None:
    """Swap the whole table in one transaction, so a failure leaves the old one."""
    try:
        SymToken.query.delete()
        db_session.bulk_insert_mappings(SymToken, rows)
        db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def master_contract_download():
    logger.info("Building the symbol list from the NSE bhavcopy")
    try:
        day = latest_trading_day()
        if day is None:
            message = (
                "No NSE end-of-day file was found for the last ten days, so the symbol "
                "list could not be built. Try again after 7 PM IST on a trading day."
            )
            logger.error(message)
            return socketio.emit(
                "master_contract_download", {"status": "error", "message": message}
            )

        table = build_symbol_table(fetch_bhavcopy("CM", day), fetch_bhavcopy("FO", day))
        _replace_symbols(table.to_dict(orient="records"))
        logger.info(f"Symbol list built from the {day:%d %b %Y} bhavcopy: {len(table)} symbols")
        return socketio.emit(
            "master_contract_download",
            {"status": "success", "message": f"Symbols loaded from NSE files of {day:%d %b %Y}"},
        )
    except ArchiveUnavailable as e:
        logger.exception(f"Symbol list download failed: {e}")
        return socketio.emit("master_contract_download", {"status": "error", "message": str(e)})
    except Exception as e:
        logger.exception(f"Error building the public symbol list: {e}")
        return socketio.emit(
            "master_contract_download",
            {"status": "error", "message": "The symbol list could not be built. See the logs."},
        )


def search_symbols(symbol, exchange):
    return SymToken.query.filter(
        SymToken.symbol.like(f"%{symbol}%"), SymToken.exchange == exchange
    ).all()
