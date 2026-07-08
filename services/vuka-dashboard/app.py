import os
import time

import pandas as pd
import plotly.express as px
import streamlit as st

import db
import whatsapp_alerts
from crypto_utils import decrypt_phone, mask_phone

st.set_page_config(page_title="Vuka Treasury", layout="wide")

VUKA_MLRO_NAME = os.environ.get("VUKA_MLRO_NAME")
VUKA_MLRO_EMAIL = os.environ.get("VUKA_MLRO_EMAIL")
MLRO_DESIGNATED = bool(VUKA_MLRO_NAME and VUKA_MLRO_EMAIL)

CORRIDORS = ["KENYA", "UGANDA", "TANZANIA", "RWANDA", "GHANA"]

st.title("Vuka Treasury")

if not db.db_exists():
    st.warning(
        f"No ledger database found yet at `{db.DB_PATH}`. It's created automatically the first "
        "time the USSD backend runs and processes a transaction."
    )
    st.stop()

tab_overview, tab_float, tab_compliance, tab_ai = st.tabs(
    ["Overview", "Float Pools & Margins", "Compliance & AML/KYC", "AI Assistant"]
)

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
with tab_overview:
    stats = db.get_summary_stats()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total transactions", stats["total_transactions"])
    c2.metric("Completed", stats["completed"])
    c3.metric("Failed", stats["failed"])
    c4.metric("Compliance holds", stats["compliance_held"])

    st.subheader("Gross volume switched, by sender's origin currency")
    st.caption(
        "Vuka is multi-origin -- senders debit in their OWN currency, not always KES -- "
        "so volume is broken down per currency rather than blended into one misleading total."
    )
    if stats["gross_volume_by_currency"]:
        vol_df = pd.DataFrame(stats["gross_volume_by_currency"])
        cols = st.columns(len(vol_df))
        for col, row in zip(cols, vol_df.itertuples()):
            col.metric(row.currency, f"{row.total:,.2f}")
    else:
        st.info("No completed transactions yet.")

    st.subheader("Revenue (Vuka's own fee income), by origin currency")
    st.caption(
        "This is Vuka's cut only (vuka_fee) -- not the combined fee charged to the sender, "
        "which also includes the settlement partner's share. Broken down by currency for the "
        "same reason as volume above."
    )
    if stats["revenue_by_currency"]:
        rev_df = pd.DataFrame(stats["revenue_by_currency"])
        st.dataframe(
            rev_df.rename(columns={
                "currency": "Currency", "confirmed": "Confirmed", "simulated": "Simulated",
                "partner_total": "Partner's cut",
            }),
            use_container_width=True, hide_index=True,
        )
        if rev_df["confirmed"].sum() == 0 and rev_df["simulated"].sum() > 0:
            st.caption(
                "All revenue so far is simulated -- no live payout/collection provider is configured yet."
            )
    else:
        st.info("No completed transactions yet.")

    if stats["pending_by_currency"]:
        st.caption("Pending (in-flight): " + ", ".join(
            f"{r['currency']} {r['pending']:,.2f}" for r in stats["pending_by_currency"]
        ))

    st.subheader("Corridor volumes (payout total, destination currency)")
    volumes = db.get_corridor_volumes()
    if volumes:
        vol_df = pd.DataFrame(volumes)
        vol_df["label"] = vol_df["corridor"].str.title() + " (" + vol_df["currency"] + ")"
        fig = px.bar(vol_df, x="label", y="total_payout", text="tx_count",
                     labels={"total_payout": "Total paid out", "label": "Corridor"})
        fig.update_traces(texttemplate="%{text} tx", textposition="outside")
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Bars are not directly comparable across corridors -- each is in its own "
            "destination currency (shown in the label), not a common unit."
        )
    else:
        st.info("No corridor transactions yet.")

    st.subheader("Revenue over time (confirmed vs simulated, by origin currency)")
    series = db.get_revenue_timeseries(days=30)
    if series:
        ts_df = pd.DataFrame(series)
        ts_df["date"] = pd.to_datetime(ts_df["day_bucket"], unit="s")
        ts_df = ts_df.melt(
            id_vars=["date", "currency"], value_vars=["confirmed", "simulated"],
            var_name="type", value_name="fee",
        )
        ts_df["series"] = ts_df["currency"] + " (" + ts_df["type"] + ")"
        fig2 = px.line(ts_df, x="date", y="fee", color="series", markers=True)
        st.plotly_chart(fig2, use_container_width=True)
        st.caption("Each line is its own currency -- values are not directly comparable across lines.")
    else:
        st.info("Not enough data yet for a time series.")

    st.subheader("Recent transactions")
    recent = db.get_recent_transactions(limit=50)
    if recent:
        df = pd.DataFrame(recent)
        df["created_at"] = pd.to_datetime(df["created_at"], unit="s")
        df["simulated"] = (df["collection_simulated"] == 1) | (df["payout_simulated"] == 1)
        st.dataframe(
            df[["created_at", "transaction_type", "corridor", "send_amount", "send_currency",
                "payout_amount", "payout_currency", "status", "simulated", "recipient_name"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No transactions yet.")

# ---------------------------------------------------------------------------
# Float pools + margin tuning
# ---------------------------------------------------------------------------
with tab_float:
    st.subheader("Float pool balances")
    pools = db.get_float_pools()
    pool_df = pd.DataFrame(pools) if pools else pd.DataFrame()

    for pool in pools:
        low = pool["balance"] <= pool["low_threshold"]
        cols = st.columns([2, 2, 2, 1])
        cols[0].markdown(f"**{pool['corridor'].title()}**")
        cols[1].metric("Balance", f"{pool['balance']:,.2f} {pool['currency']}")
        cols[2].metric("Low threshold", f"{pool['low_threshold']:,.2f} {pool['currency']}")
        if low:
            cols[3].error("LOW")
            alert_key = f"alert_{pool['corridor']}"
            if st.button("Send WhatsApp alert", key=alert_key):
                result = whatsapp_alerts.send_low_float_alert(
                    pool["corridor"], pool["balance"], pool["currency"], pool["low_threshold"]
                )
                if result["simulated"]:
                    st.info(f"Simulated alert (WhatsApp not configured): {result['message']}")
                elif result.get("error"):
                    st.error("Alert failed to send -- check WhatsApp API credentials/logs.")
                else:
                    st.success("Alert sent.")
        else:
            cols[3].success("OK")

    st.divider()
    st.subheader("Live margin tuning")
    st.caption(
        "Adjusts the margin (in basis points) built into the quoted FX rate for each corridor. "
        "Takes effect immediately on the next quote -- the USSD backend reads this live from the "
        "shared settings table, no redeploy needed."
    )
    for corridor in CORRIDORS:
        current = db.get_margin_bps(corridor)
        new_value = st.slider(
            f"{corridor.title()} margin (bps)", min_value=0, max_value=1000,
            value=int(current), step=10, key=f"margin_{corridor}",
        )
        if new_value != int(current):
            db.set_margin_bps(corridor, new_value)
            st.toast(f"{corridor.title()} margin updated to {new_value} bps")

# ---------------------------------------------------------------------------
# Compliance & AML/KYC
# ---------------------------------------------------------------------------
with tab_compliance:
    if MLRO_DESIGNATED:
        st.success(f"MLRO designated: {VUKA_MLRO_NAME} ({VUKA_MLRO_EMAIL})")
    else:
        st.error(
            "No Money Laundering Reporting Officer is currently designated. "
            "Set VUKA_MLRO_NAME and VUKA_MLRO_EMAIL to designate one. "
            "No placeholder name is shown here as a substitute."
        )

    st.subheader("Open compliance flags")
    open_flags = db.get_open_flags()

    if not open_flags:
        st.info("No open compliance flags.")
    else:
        for flag in open_flags:
            severity_color = "red" if flag["severity"] == "high" else "orange"
            with st.expander(
                f":{severity_color}[{flag['severity'].upper()}] {flag['rule']} — "
                f"{flag['corridor'] or 'merchant payment'} — {flag['send_amount']} {flag['send_currency']}"
            ):
                st.write(flag["note"])
                if flag["simulated"]:
                    st.caption("This flag was raised by a simulated rule (see note above for detail).")
                sender = decrypt_phone(flag.get("sender_phone") or "")
                recipient = decrypt_phone(flag.get("recipient_phone") or "") if flag.get("recipient_phone") else None
                st.text(f"Sender: {mask_phone(sender)}")
                if recipient:
                    st.text(f"Recipient: {mask_phone(recipient)}")
                st.caption(f"Transaction ID: {flag['transaction_id']}")

                resolver = st.text_input("Your name/email", key=f"resolver_{flag['id']}")
                bcol1, bcol2 = st.columns(2)
                if bcol1.button("Clear", key=f"clear_{flag['id']}", disabled=not resolver):
                    db.resolve_flag(flag["id"], "cleared", resolver)
                    st.rerun()
                if bcol2.button("Escalate (SAR)", key=f"escalate_{flag['id']}", disabled=not resolver):
                    db.resolve_flag(flag["id"], "escalated", resolver)
                    st.rerun()

    st.divider()
    st.subheader("Full audit trail")
    all_flags = db.get_all_flags(limit=500)
    if all_flags:
        audit_df = pd.DataFrame(all_flags)
        audit_df["created_at"] = pd.to_datetime(audit_df["created_at"], unit="s")
        st.dataframe(
            audit_df[["created_at", "rule", "severity", "status", "note", "simulated", "corridor"]],
            use_container_width=True,
            hide_index=True,
        )

        escalated = [f for f in all_flags if f["status"] == "escalated"]
        if escalated:
            csv_data = db.export_flags_csv(escalated)
            st.download_button(
                "Export escalated flags (batch CSV)",
                data=csv_data,
                file_name=f"vuka_sar_export_{int(time.time())}.csv",
                mime="text/csv",
            )
            st.caption(
                "This is a local export of escalated flags, not a submission to any "
                "Financial Reporting Centre or regulator."
            )

            st.markdown("**Draft SAR documents (one per escalated flag)**")
            for f in escalated:
                sar_text = db.generate_sar_document(f)
                readable_time = pd.to_datetime(f["created_at"], unit="s").strftime("%Y-%m-%d %H:%M")
                st.download_button(
                    f"Draft SAR — {f['rule']} — {readable_time}",
                    data=sar_text,
                    file_name=f"vuka_sar_draft_{f['id'][:8]}.txt",
                    mime="text/plain",
                    key=f"sar_{f['id']}",
                )
    else:
        st.info("No compliance flags have been raised yet.")

# ---------------------------------------------------------------------------
# AI Assistant
# ---------------------------------------------------------------------------
with tab_ai:
    st.subheader("Natural-language treasury assistant")
    try:
        # ai_treasury.py lives in the backend package (services/ussd-backend/app/)
        # since it uses ledger.py directly to build its privacy-safe summary.
        # Reach it via sys.path rather than duplicating ledger access here.
        import sys as _sys
        import os as _os

        _backend_path = _os.path.join(_os.path.dirname(__file__), "..", "ussd-backend")
        if _backend_path not in _sys.path:
            _sys.path.insert(0, _backend_path)
        from app import ai_treasury

        st.caption(
            "Simulated (Gemini not configured)" if ai_treasury.SIMULATION_MODE_GEMINI else "Live via Gemini"
        )
        question = st.text_input("Ask about your treasury data", key="ai_question")
        if st.button("Ask", key="ai_ask") and question:
            with st.spinner("Thinking..."):
                answer = ai_treasury.ask(question)
            st.write(answer)
    except ImportError:
        st.info(
            "The AI treasury assistant module isn't reachable -- make sure the "
            "ussd-backend service directory sits alongside this dashboard directory."
        )
