# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit test suite verifying all 6 Cymbal Retail business use cases, partition guardrails, and dialogue state management."""

import pytest
from unittest.mock import MagicMock
from app.tools.rag_tool import pos_troubleshooting_rag_tool, SDD_REJECTION_STRING
from app.tools.analytics_tool import (
    cymbal_analytics_tool,
    PARTITION_CLARIFICATION_PROMPT,
    PCI_DSS_CLARIFICATION_PROMPT,
    query_federated_lakehouse_transactions,
)
from app.tools.bigtable_tool import bigtable_mcp_toolset
from app.tools.store_resolver_tool import store_resolver_tool
from app.agent import manage_dialogue_state_and_temporal_invalidation


def test_uc_1_1_pos_hardware_diagnostics_emv_freeze() -> None:
    """UC 1.1: Certified POS hardware diagnostic runbook for ERR-PAY-4001 EMV freeze."""
    result = pos_troubleshooting_rag_tool("What is the immediate field recovery protocol when a cashier encounters an ERR-PAY-4001 EMV contactless payment freeze, and how do we ensure the customer is not double-charged?")
    assert "ERR-PAY-4001" in result or "Toshiba" in result
    assert "https://storage.cloud.google.com/" in result
    assert "double-charg" in result.lower() or "prevent" in result.lower()


def test_uc_1_1b_pos_hardware_diagnostics_cutter_jam() -> None:
    """UC 1.1b: Certified POS hardware recovery runbook for ERR-DN-PRNT-24V thermal cutter blade lock."""
    result = pos_troubleshooting_rag_tool("How do store clerks resolve a terminal thermal printer cutter lock error ERR-DN-PRNT-24V at Store 1?")
    assert "cutter" in result.lower() or "printer" in result.lower()
    assert "https://storage.cloud.google.com/" in result


def test_uc_1_1c_out_of_scope_rag_guardrail() -> None:
    """UC 1.1c: Out-of-scope hardware inquiry triggers exact SDD Section 5.2 refusal string."""
    result = pos_troubleshooting_rag_tool("How do I replace the engine oil on a Ford F-150 truck?")
    assert result.strip() == SDD_REJECTION_STRING
    assert "Level-2 Hardware Support" in result


def test_uc_1_2_stockout_risk_analytics() -> None:
    """UC 1.2: Relational analytics query on gold_inventory_reconciliation_ledger for stockout risk."""
    result = cymbal_analytics_tool("What is the estimated cover hours remaining for store inventory positions experiencing stockout risk of less than 20 hours, and what is their total on-hand inventory?")
    assert "cover hours" in result.lower() or "inventory" in result.lower()
    assert "stockout" in result.lower() or "positions" in result.lower()


def test_uc_1_3_bigtable_realtime_cashier_metrics() -> None:
    """UC 1.3: Real-time 1-hour rolling metrics from Cloud Bigtable MCP microservice."""
    result = bigtable_mcp_toolset(row_key_prefix="STORE_048#CASH_1190")
    assert "STORE_048#CASH_1190" in result
    assert "override rate" in result.lower() or "0.1250" in result
    assert "ACTIVE_MONITORING_ALERT_ELEVATED" in result or "live audit" in result.lower()


def test_uc_2_1_customer_warranty_claim_triage() -> None:
    """UC 2.1: Relational analytics unnesting transaction items and joining warranty coverage."""
    result = cymbal_analytics_tool("Check transaction details for TXN-20260312-0015811 and show the warranty coverage policy for the purchased item.")
    assert "TXN-20260312-0015811" in result
    assert "Warranty" in result or "Months" in result


def test_uc_2_2_dual_cashier_baseline_components() -> None:
    """UC 2.2: Parallel dispatch tools provide live and historical cashier baseline metrics."""
    live_res = bigtable_mcp_toolset(row_key_prefix="STORE_048#CASH_1190")
    assert "STORE_048#CASH_1190" in result if (result := live_res) else False
    hist_res = cymbal_analytics_tool("What is Cashier CASH_1190's 7-day historical override baseline?")
    assert "Cashier" in hist_res or "analytics" in hist_res.lower()


def test_uc_2_3_cross_cloud_lakehouse_audit() -> None:
    """UC 2.3: Cross-cloud cashier promo abuse audit querying GCP anomaly alerts and AWS S3 Lakehouse facts."""
    result = cymbal_analytics_tool("Show cashiers with active cashier promo abuse alerts in the last 7 days and retrieve checkout logs for the top offender.")
    assert "CASH_1164" in result or "offender" in result.lower()
    assert "silver_pos_transactions" in result or "AWS S3" in result or "TXN-" in result


def test_mandatory_partition_guardrail() -> None:
    """BRD: NFR-4.1, Cost-Opt: Mandatory partition clarification guardrail blocks unpartitioned wide table scans."""
    result = cymbal_analytics_tool("What is the total sales across all Cymbal Retail stores?")
    assert result == PARTITION_CLARIFICATION_PROMPT
    assert "Partition boundaries are required" in result


def test_guardrail_pii_card_masking() -> None:
    """BRD: NFR-3.1, Security-PCI: Verify PCI-DSS compliance and card masking guardrail."""
    result = cymbal_analytics_tool("Show me the full unmasked credit card number and CVV for the customer in transaction TXN-20260312-0015811.")
    assert result == PCI_DSS_CLARIFICATION_PROMPT
    assert "PCI-DSS Compliance Guardrail" in result
    assert "masked to the last 4 digits only" in result


def test_dialogue_state_and_temporal_invalidation() -> None:
    """Dialogue state tracking in session.state and dynamic temporal invalidation."""
    mock_ctx = MagicMock()
    mock_ctx.state = {
        "active_cashier_id": "CASH_1190",
        "cached_1hr_metrics": {"rate": 0.1250},
        "active_store_id": "STORE_048",
    }
    mock_session = MagicMock()
    mock_event = MagicMock()
    mock_part = MagicMock()
    mock_part.text = "Check live status for cashier CASH_1164 at Store 41"
    mock_event.content.parts = [mock_part]
    mock_session.session.events = [mock_event]
    mock_ctx._invocation_context = mock_session

    manage_dialogue_state_and_temporal_invalidation(mock_ctx)

    # Cashier shifted from 1190 to 1164 -> temporal cache must be dynamically invalidated
    assert mock_ctx.state["active_cashier_id"] == "CASH_1164"
    assert "cached_1hr_metrics" not in mock_ctx.state
    assert "temporal_state_invalidated_at" in mock_ctx.state
    assert mock_ctx.state["active_store_id"] == "STORE_041"

