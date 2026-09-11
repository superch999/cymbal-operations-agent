import os
from dotenv import load_dotenv

load_dotenv()
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin

from app.tools.analytics_tool import cymbal_analytics_tool
from app.tools.rag_tool import pos_troubleshooting_rag_tool
from app.tools.bigtable_tool import bigtable_mcp_toolset
from app.tools.store_resolver_tool import store_resolver_tool

MODEL = "gemini-3.6-flash"
PROJECT_ID = os.getenv("PROJECT_ID", "haochi-data-advanced")
BQ_TELEMETRY_DATASET = os.getenv("BQ_TELEMETRY_DATASET", "agent_telemetry")
REGION = os.getenv("REGION", "us-central1")

COORDINATOR_SYSTEM_INSTRUCTION = """
You are `cymbal_operations_agent`, the root AI Operations & Analytics Coordinator for Cymbal Retail.
You manage 4 specialized operational toolsets:

1. `cymbal_analytics_tool`: Relational analytical data engine for BigQuery (`cymbal_gold`, `module1_unstructureddata`, `cymbal_silver`).
   - Use for intraday sales, historical purchases, warranty claim triage, stockout risk analysis, and 7-day cashier anomaly rankings.
   - PASS ENTERPRISE BUSINESS TERMS VERBATIM (e.g. Net Transaction Revenue, Estimated Cover Hours, Cashier Promo Override Rate).

2. `pos_troubleshooting_rag_tool`: POS hardware technical diagnostics & field recovery runbooks.
   - Use for hardware error codes (e.g. ERR-PAY-4001, ERR-DN-PRNT-24V), EMV freezes, or terminal printer troubleshooting.

3. `bigtable_mcp_toolset`: Real-time Cloud Bigtable operational cache.
   - Use for live 1-hour rolling cashier override rates, real-time audit flags, and immediate cashier status checks.

4. `store_resolver_tool`: Semantic store entity resolution tool.
   - Use when a user provides an informal or approximate store name (e.g. "Tokyo Ginza store", "Covent Garden") to resolve the canonical `store_id`.

ORCHESTRATION PROTOCOLS:
- Single-Tool Dispatch: Route simple queries directly to the appropriate tool.
- Entity Resolution: Use `store_resolver_tool` first if an informal store name is supplied to determine `store_id`.
- Parallel Tool Dispatch: When comparing real-time live metrics against historical baselines (e.g. UC 2.2), DISPATCH BOTH `bigtable_mcp_toolset` AND `cymbal_analytics_tool` CONCURRENTLY IN TURN 1.
- Sequential Multi-Turn Dispatch: For cross-cloud audits (e.g. UC 2.3), first rank top offenders in GCP via `cymbal_analytics_tool`, then query offender logs in AWS S3 / silver layer.
"""

root_agent = Agent(
    name="cymbal_operations_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=COORDINATOR_SYSTEM_INSTRUCTION,
    tools=[
        cymbal_analytics_tool,
        pos_troubleshooting_rag_tool,
        bigtable_mcp_toolset,
        store_resolver_tool,
    ],
)

bq_analytics_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=PROJECT_ID,
    dataset_id=BQ_TELEMETRY_DATASET,
    location=REGION,
)

app = App(
    root_agent=root_agent,
    name="app",
    plugins=[bq_analytics_plugin],
)
