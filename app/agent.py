import logging
import os
import re
import time
from dotenv import load_dotenv

load_dotenv()
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin

from app.tools.analytics_tool import cymbal_analytics_tool
from app.tools.rag_tool import pos_troubleshooting_rag_tool
from app.tools.bigtable_tool import bigtable_mcp_toolset
from app.tools.store_resolver_tool import store_resolver_tool

logger = logging.getLogger(__name__)

MODEL = "gemini-3.6-flash"
PROJECT_ID = os.getenv("PROJECT_ID", "haochi-data-advanced")
BQ_TELEMETRY_DATASET = os.getenv("BQ_TELEMETRY_DATASET", "agent_telemetry")
REGION = os.getenv("REGION", "us-central1")


def manage_dialogue_state_and_temporal_invalidation(callback_context: CallbackContext) -> None:
    """Maintains multi-turn dialogue state in session.state and enforces dynamic temporal state invalidation.

    Tracks:
    - active_store_id: Canonical store identifier (e.g. STORE_048)
    - active_cashier_id: Cashier identifier (e.g. CASH_1190)
    - active_transaction_id: Transaction identifier (e.g. TXN-20260312-0015811)
    - active_error_code: Hardware error code (e.g. ERR-PAY-4001)

    Dynamic Temporal State Invalidation:
    - If user switches to a new cashier or store, immediately invalidates stale 1-hour rolling metrics.
    - If user asks for a new time period, clears temporal cache to prevent state leakage across turns.
    """
    state = callback_context.state
    current_time = time.time()

    # Extract user message if present in events
    message_text = ""
    session = getattr(callback_context, "_invocation_context", None)
    if session and hasattr(session, "session") and getattr(session.session, "events", None):
        for ev in reversed(session.session.events):
            if getattr(ev, "content", None) and getattr(ev.content, "parts", None):
                for p in ev.content.parts:
                    if getattr(p, "text", None):
                        message_text = p.text
                        break
            if message_text:
                break

    if not message_text:
        return

    cashier_match = re.search(r'\b(?:CASH_|cashier\s+)(\d+)\b', message_text, re.IGNORECASE)
    store_match = re.search(r'\b(?:STORE_|store\s+)(\d+)\b', message_text, re.IGNORECASE)
    txn_match = re.search(r'\b(TXN-\d+-\d+)\b', message_text, re.IGNORECASE)
    error_match = re.search(r'\b(ERR-[A-Z0-9-]+)\b', message_text, re.IGNORECASE)

    # Dynamic Temporal Invalidation on Cashier Shift
    if cashier_match:
        new_cashier = f"CASH_{cashier_match.group(1)}"
        old_cashier = state.get("active_cashier_id")
        if old_cashier and old_cashier != new_cashier:
            state.pop("cached_1hr_metrics", None)
            state.pop("cached_override_rate", None)
            state["temporal_state_invalidated_at"] = current_time
        state["active_cashier_id"] = new_cashier

    # Dynamic Temporal Invalidation on Store Shift
    if store_match:
        store_num = store_match.group(1).zfill(3)
        new_store = f"STORE_{store_num}"
        old_store = state.get("active_store_id")
        if old_store and old_store != new_store:
            state.pop("cached_1hr_metrics", None)
            state.pop("cached_inventory_cover", None)
            state["temporal_state_invalidated_at"] = current_time
        state["active_store_id"] = new_store

    if txn_match:
        state["active_transaction_id"] = txn_match.group(1).upper()

    if error_match:
        state["active_error_code"] = error_match.group(1).upper()

    state["last_interaction_timestamp"] = current_time


COORDINATOR_SYSTEM_INSTRUCTION = """
You are `cymbal_operations_agent`, the root AI Operations & Analytics Coordinator for Cymbal Retail.
You orchestrate 4 specialized operational toolsets to assist store leads, technicians, and loss-prevention auditors:

1. `cymbal_analytics_tool`: BigQuery Conversational Data Agent for relational analytics across structured Gold tables (`pos_transactions_gold`, `pos_anomaly_alerts`, `gold_inventory_reconciliation_ledger`, `historical_transactional_data`), extracted warranty policies (`warranty_generic_sections_extracted`), and federated AWS S3 tables (`cymbal-lakehouse.elevate_data.silver_pos_transactions`).
   - Always pass standardized enterprise business terms verbatim (e.g. Net Transaction Revenue, Total On-Hand Inventory, Estimated Cover Hours, Cashier Promo Override Rate).

2. `pos_troubleshooting_rag_tool`: Vector similarity search with adjacent context window stitching over POS hardware technical manuals in BigQuery (`cymbal_gold.pos_manual_chunk_embeddings`).
   - Use for hardware error codes (e.g. ERR-PAY-4001, ERR-DN-PRNT-24V) and terminal maintenance runbooks.
   - Always include clickable HTTPS GCS documentation links in your response.

3. `bigtable_mcp_toolset`: Live sub-second 1-hour rolling metrics and audit status flags from Cloud Bigtable (`operations-db:cashier_realtime_alerts`) via the Cloud Run MCP Toolbox microservice.
   - Use row key prefix format `STORE_<ID>#CASH_<ID>` (e.g. `STORE_048#CASH_1190`).

4. `store_resolver_tool`: Semantic store entity resolution tool.
   - Use when a user provides an informal or approximate store name (e.g. "Tokyo Ginza store", "Covent Garden") to resolve canonical `store_id`.

ORCHESTRATION & DISPATCH PROTOCOLS:
- SINGLE-TOOL DISPATCH: Route direct inquiries to the appropriate tool. For ANY technical, maintenance, or repair question (including out-of-domain vehicle/machinery repair like Ford F-150 oil change), invoke `pos_troubleshooting_rag_tool` first.
- PARALLEL TOOL DISPATCH: When comparing live real-time cashier metrics against 7-day historical baselines (e.g. UC 2.2), invoke `bigtable_mcp_toolset` AND `cymbal_analytics_tool` CONCURRENTLY in Turn 1.
- SEQUENTIAL MULTI-TURN DISPATCH: When auditing cross-cloud promo abuse offenders (e.g. UC 2.3), first call `cymbal_analytics_tool` to rank top promo abuse cashiers in GCP BigQuery (`pos_anomaly_alerts`), then retrieve historical checkout logs from AWS S3 (`silver_pos_transactions` in `cymbal-lakehouse`) for the top offender.
- PCI-DSS PAYMENT SECURITY & PII CARD MASKING GUARDRAIL (BRD: NFR-3.1, Security-PCI): Under Cymbal Retail security policy and PCI-DSS compliance standards, payment card account numbers (PAN) are strictly tokenized to the last 4 digits only, and CVV / CID security codes are NEVER stored or exposed. If a user asks for full unmasked 16-digit credit card numbers, PAN, or CVV for any transaction or customer, you MUST enforce PCI-DSS compliance. The output must explicitly contain the PCI-DSS compliance guardrail notice:
  "[PCI-DSS Compliance Guardrail]: Full credit card numbers and security codes (CVV) cannot be displayed. Under Cymbal Retail security policy and PCI-DSS standards, payment details are strictly tokenized and masked to the last 4 digits only."
  and MUST strictly exclude any full 16-digit PAN numbers.
- MANDATORY PARTITION PRUNING & CLARIFICATION GUARDRAIL (BRD: NFR-4.1, Cost-Opt): All enterprise transaction ledgers are strictly partitioned by date. To prevent prohibitive table scan billing on unbound enterprise transaction queries, if the user asks for total sales or aggregate revenue across stores without specifying a date or timeframe, DO NOT execute an unpartitioned BigQuery SQL query. Prompt the user for a specific date window before executing BigQuery SQL:
  "To calculate total sales across all Cymbal Retail stores accurately, please specify the target date or date range (e.g. today intraday, past 7 days, or current quarter). Partition boundaries are required to optimize query performance."
- STRICT RAG SAFETY GUARDRAIL: If `pos_troubleshooting_rag_tool` returns the certified refusal message:
  "I cannot find certified warranty or repair rules for this specific error code in our technical repository. Please contact Level-2 Hardware Support."
  Reply ONLY with that exact refusal sentence. Do NOT add unverified conversational advice, disclaimers, or automotive instructions.
- DIALOGUE STATE MANAGEMENT & TEMPORAL INVALIDATION: Dialogue context is maintained across turns via `session.state`. If the user shifts to a new cashier or store, stale 1-hour metrics and temporal caches are dynamically invalidated.
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
    before_agent_callback=manage_dialogue_state_and_temporal_invalidation,
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

