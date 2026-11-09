import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    anthropic_model = os.getenv("ANALYST_MODEL", "claude-sonnet-5-5")
    warehouse_dsn = os.getenv(
        "WAREHOUSE_DSN", "postgresql://analyst_ro:analyst_ro_pw@localhost:5433/warehouse"
    )
    exposed_schemas = [s.strip() for s in os.getenv("EXPOSED_SCHEMAS", "public").split(",") if s.strip()]
    statement_timeout_ms = int(os.getenv("STATEMENT_TIMEOUT_MS", "15000"))
    max_rows = int(os.getenv("MAX_ROWS", "1000"))
    max_plan_cost = float(os.getenv("MAX_PLAN_COST", "5000000"))
    app_db_path = os.getenv("APP_DB_PATH", "./data/app.db")
    max_agent_steps = 14
    max_clarification_rounds = 2  # per user question
    tenant_id = "default"  # single tenant for now; kept in tables so you can generalize later


settings = Settings()
