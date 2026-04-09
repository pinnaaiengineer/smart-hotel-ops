import json
import textwrap
from datetime import date

import streamlit as st
import anthropic

from src import config
from src.pms import PMS
from src.agent import HotelEmailAgent
from src.executor import execute_plan
# --- Page Configuration ---
st.set_page_config(
    page_title="Hotel Agent Dashboard",
    page_icon="🏨",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Custom CSS for Minimalist Designer UI ---
st.markdown("""
    <style>
    /* Styling adjustments */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    
    .main-header {
        font-size: 28px;
        font-weight: 600;
        color: #1E293B;
        margin-bottom: 20px;
        padding-bottom: 10px;
        border-bottom: 1px solid #E2E8F0;
    }
    
    .section-header {
        font-size: 18px;
        font-weight: 600;
        color: #334155;
        margin-top: 15px;
        margin-bottom: 10px;
    }
    
    /* Soften the expanders */
    div[data-testid="stExpander"] {
        border-radius: 8px;
        border: 1px solid #E2E8F0;
        box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
    }
    
    /* Make the action buttons pop */
    .stButton>button {
        width: 100%;
        border-radius: 8px;
        font-weight: 500;
    }
    
    /* Alert styling overrides if needed */
    .stAlert {
        border-radius: 8px;
    }
    
    /* Style tool calls JSON nicely */
    .json-block {
        background-color: #F8FAFC !important;
        border: 1px solid #E2E8F0 !important;
        border-radius: 8px !important;
    }
    </style>
""", unsafe_allow_html=True)

# --- Session State Initialization ---
if 'pms' not in st.session_state:
    st.session_state.pms = PMS(config.DATA_PATH)
if 'current_date' not in st.session_state:
    st.session_state.current_date = date.fromisoformat(config.MOCK_CURRENT_DATE)
if 'plan_result' not in st.session_state:
    st.session_state.plan_result = None
if 'exec_result' not in st.session_state:
    st.session_state.exec_result = None
if 'active_scenario' not in st.session_state:
    st.session_state.active_scenario = None

# Variables to sync between quick forms and the main form
if "form_sender" not in st.session_state:
    st.session_state.form_sender = ""
if "form_body" not in st.session_state:
    st.session_state.form_body = ""


# --- Sidebar: Inbound Feed ---
with st.sidebar:
    st.markdown("### 📥 Email Inbox")
    
    if st.button("➕ Receive New Email", use_container_width=True):
        st.session_state.plan_result = None
        st.session_state.exec_result = None
        st.session_state.form_sender = ""
        st.session_state.form_body = ""
        st.rerun()
    # Using individual widgets instead of a form to prevent Enter-to-Submit collision
    sender_email = st.text_input("From:", value=st.session_state.form_sender, placeholder="guest@example.com")
    email_body = st.text_area("Body:", value=st.session_state.form_body, height=250, placeholder="Hello, I'd like to book...")
    
    # Use a simple button which requires a click to submit
    if st.button("Process Email ", type="primary", use_container_width=True):
            if not config.ANTHROPIC_API_KEY:
                st.error("Missing ANTHROPIC_API_KEY in .env")
            elif not email_body.strip():
                st.warning("Please provide an email body.")
            else:
                # Run the Agent
                st.session_state.exec_result = None # Reset previous result
                with st.spinner("AI Agent is analyzing and planning..."):
                    try:
                        agent = HotelEmailAgent(pms=st.session_state.pms, current_date=st.session_state.current_date)
                        plan = agent.plan(email_body, sender_email)
                        st.session_state.plan_result = plan
                        
                        # Sync back the state so the form retains what was typed
                        st.session_state.form_sender = sender_email
                        st.session_state.form_body = email_body

                        # AUTO-EXECUTION if in autonomous mode and no review required
                        if config.APPROVAL_MODE == "autonomous" and not plan.requires_human_review:
                            action_dicts = [
                                {"action": a.action, "params": a.params, "description": a.description}
                                for a in plan.action_plan
                            ]
                            if action_dicts:
                                result = execute_plan(st.session_state.pms, action_dicts)
                                st.session_state.exec_result = result
                            else:
                                st.session_state.exec_result = "SENT_NO_CHANGES"
                        
                    except anthropic.APIError as e:
                        st.error(f"Anthropic API Error: {e}")
                    except Exception as e:
                        st.error(f"An unexpected error occurred: {e}")

# --- Main Dashboard ---
st.markdown('<div class="main-header">🏨 Hotel AI Agent Dashboard</div>', unsafe_allow_html=True)

plan = st.session_state.plan_result
exec_result = st.session_state.exec_result

if not plan:
    st.markdown("""
    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 4rem 2rem; text-align: center;">
        <div style="font-size: 48px; margin-bottom: 1rem;">🏨</div>
        <h2 style="color: #1E293B; font-weight: 600; margin-bottom: 0.5rem;">Welcome to the Agent Dashboard</h2>
        <p style="color: #64748b; font-size: 15px; margin-bottom: 2rem;">
            Please compose or select an email from the <b>Inbox</b> panel on the left to begin.
        </p>
    </div>
    """, unsafe_allow_html=True)
    
    # Just a visual interactive element to make the UI feel alive if they interact with the main view
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        if st.button("✨ Show Me How", use_container_width=True):
            st.toast("Look at the dark panel on the left to paste a guest email!", icon="👈")
else:
    # The Workspace split
    col1, col2 = st.columns([1, 1], gap="medium")
    
    with col1:
        st.markdown('<div class="section-header">🧠 Proposed Action Plan</div>', unsafe_allow_html=True)
        st.markdown("#### Proposed PMS Actions")
        if plan.action_plan:
            for step in plan.action_plan:
                with st.container(border=True):
                    st.markdown(f"**{ step.action.upper() }**")
                    st.caption(step.description)
                    st.json(step.params)
        else:
            st.success("No write actions required. This is a read-only request.")

    with col2:
        st.markdown('<div class="section-header">✉️ Drafted Reply & Execution</div>', unsafe_allow_html=True)
        
        # Human Review Banner
        if plan.requires_human_review:
            st.error("⚠️ **REQUIRES HUMAN REVIEW**")
            st.write(f"**Reason:** {plan.review_reason}")
            
        # Draft Email Viewer
        st.markdown("#### Email Draft")
        st.text_area("Review the AI drafted response:", value=plan.draft_reply, height=300, disabled=True)
        
        # Execution Controls
        if not exec_result:
            st.markdown("#### Action required")
            btn1, btn2 = st.columns(2)
            
            with btn1:
                # Approve & Execute
                if plan.action_plan and not plan.requires_human_review:
                    if st.button("✅ Approve & Execute Changes", type="primary"):
                        with st.spinner("Executing PMS updates..."):
                            action_dicts = [
                                {"action": a.action, "params": a.params, "description": a.description}
                                for a in plan.action_plan
                            ]
                            result = execute_plan(st.session_state.pms, action_dicts)
                            st.session_state.exec_result = result
                            st.rerun()
                elif plan.action_plan and plan.requires_human_review:
                    st.button("⚙️ Execute Requires Override", disabled=True)
                else:
                    if st.button("✅ Approve Draft & Send (No changes)"):
                        st.session_state.exec_result = "SENT_NO_CHANGES" # Mock state
                        st.rerun()

            with btn2:
                if st.button("❌ Reject & Discard"):
                    st.session_state.plan_result = None
                    st.rerun()
        
        else:
             # Show Execution Results if already executed
             if isinstance(exec_result, str):
                 st.success("Email simulated as sent. No PMS changes were required.")
             else:
                 st.markdown("#### Execution Results")
                 if exec_result.all_succeeded:
                     st.success("All actions executed successfully.")
                 else:
                     st.warning("Some actions failed. Check details below.")
                 
                 for res in exec_result.results:
                     with st.container(border=True):
                         status = "✅" if res.success else "❌"
                         st.markdown(f"{status} **{res.action}**")
                         if res.success:
                             st.json(res.result)
                         else:
                             st.error(res.error)
