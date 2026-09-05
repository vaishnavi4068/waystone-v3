"""Env for the IBKR dump CLI and dashboard GCS reader."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class IbkrSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    ib_host: str = "127.0.0.1"
    ib_port: int = 4001
    ib_client_id: int = 99
    ibkr_reports_bucket: str = ""
    ibkr_reports_local_dir: str = ""
    ibkr_ledger_dir: str = "./reports/ledger"
    ibkr_client_books: str = ""
    ibkr_paper: bool = False
    ibkr_kpi_nav: float = 100_000.0
    ibkr_kpi_contracts: float = 1.0
    ibkr_kpi_multiplier: float = 100.0
    ibkr_kpi_slippage: float = 0.02
