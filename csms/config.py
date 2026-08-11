"""Central configuration.

Every tunable lives here so no other module reaches for an environment
variable on its own. Override any field with an env var named
``CSMS_<FIELD>``, or via a .env file.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CSMS_", env_file=".env", extra="ignore"
    )

    # -- Database -----------------------------------------------------------
    database_path: Path = DATA_DIR / "csms.db"
    db_pool_size: int = 5
    sqlite_busy_timeout_ms: int = 5000

    # -- HTTP / WebSocket server --------------------------------------------
    host: str = "0.0.0.0"
    port: int = 9000
    ocpp_path_prefix: str = "/ocpp"
    dashboard_ws_path: str = "/ws/dashboard"
    cors_origins: list[str] = ["http://localhost:5173"]

    # -- OCPP behaviour -----------------------------------------------------
    subprotocol: str = "ocpp1.6"
    # How long to wait for a CALLRESULT before declaring a CALL timed out. The
    # spec leaves this to the implementation and asks that we account for slow
    # links; 30 s is comfortable for GPRS-attached hardware.
    call_timeout_seconds: float = 30.0
    heartbeat_interval_seconds: int = 300
    meter_value_sample_interval_seconds: int = 10
    # Reject a connection whose identity is not already in the database.
    reject_unknown_charge_points: bool = False

    # -- Charging control ----------------------------------------------------
    # Stack level for the 0 W pause profile. Kept high so it wins against any
    # load-management profile installed at a lower level.
    pause_profile_stack_level: int = 10
    pause_profile_id_base: int = 9000
    scheduler_tick_seconds: float = 1.0

    @property
    def data_dir(self) -> Path:
        return self.database_path.parent


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
