"""
The Lighthouse — Countercurrent.ai v3
Cultural Intelligence Engine for Strategy Teams

Architecture:
  - Claude (Anthropic)   → strategic content generation (LLM)
  - Gemini embeddings    → vector search (unchanged)
  - Pinecone             → vector database (unchanged)
  - Apify + Reddit + RSS → data ingestion (unchanged)
  - SendGrid             → email dispatch (unchanged)

Run:
    streamlit run app.py
"""

import os
import json
import uuid
import html as html_mod
import re as _re_global
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Optional

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Secrets injection (Streamlit Cloud) ────────────────────────────────────────
try:
    for key, value in st.secrets.items():
        if key not in os.environ:
            os.environ[key] = str(value)
except Exception:
    pass

# ── Supabase persistence layer ─────────────────────────────────────────────────
import db as _db

# ── Users & passwords ──────────────────────────────────────────────────────────
# Passwords can be overridden via .env: PASS_MARCELO=outra_senha etc.
USERS = {
    "Marcelo": os.environ.get("PASS_MARCELO", "Marcelo123"),
    "Marco":   os.environ.get("PASS_MARCO",   "Marco123"),
    "Pat":     os.environ.get("PASS_PAT",      "Pat123"),
    "Joao":    os.environ.get("PASS_JOAO",     "Joao123"),
}

# User avatar colors
USER_COLORS = {
    "Marcelo": "#0a7d8c",
    "Marco":   "#1a6b4a",
    "Pat":     "#8a3a8c",
    "Joao":    "#0a4a6e",
}

def e(text) -> str:
    """HTML-escape for safe injection."""
    return html_mod.escape(str(text))


def _extract_json(raw: str) -> dict:
    """Parse a Claude JSON response, tolerating markdown fences and the
    occasional truncated/odd-character response.

    Raises json.JSONDecodeError (with the *original* text in the message)
    if the result still isn't valid JSON — callers can catch that to show
    the raw text for debugging rather than just a cryptic "Expecting ','"
    message.
    """
    raw = raw.strip()
    if "```" in raw:
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Most common real-world failure: the response was cut off mid-field
    # because max_tokens was reached, leaving a dangling string/array/object.
    # Try dropping trailing lines one at a time until what's left can be
    # closed off into valid JSON.
    lines = raw.splitlines()
    for n in range(len(lines) - 1, 0, -1):
        candidate = "\n".join(lines[:n]).rstrip().rstrip(",")
        if not candidate:
            continue
        stack: list = []
        in_str = False
        esc = False
        for ch in candidate:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
        if not stack and not in_str:
            continue  # already balanced — wouldn't have failed at full length
        closer = ('"' if in_str else "") + "".join("}" if c == "{" else "]" for c in reversed(stack))
        try:
            return json.loads(candidate + closer)
        except json.JSONDecodeError:
            continue

    # Couldn't repair — re-raise with the original (unrepaired) text so the
    # caller can show it for debugging.
    raise json.JSONDecodeError("Unrecoverable JSON", raw, 0)


# ── Client View — accounts & permission gating ─────────────────────────────────
# Clients get their own login, scoped to a read-only, polished view of the
# dispatch. Internal sections (Project Board, Signal Lab, Vision Map) are never
# shown to clients. A few "deeper" analytical sections can be switched on
# per-client below.
CLIENT_ACCESS_PATH = "data/client_access.json"

CLIENT_PERM_DEFS = [
    ("competitive_pulse", "⚔ Competitive Pulse — charts comparing brand vs competitors"),
    ("topic_map",         "◎ Topic / Signal Map — interactive force-directed map"),
    ("momentum",          "📈 Momentum Tracker — topic evolution over time"),
    ("signal_volume",     "▦ Signal Volume Analytics — raw signal counts by source"),
]
CLIENT_PERM_DEFAULTS = {key: False for key, _ in CLIENT_PERM_DEFS}

# ── Client account helpers — delegated to db.py ───────────────────────────────
load_client_accounts  = _db.load_client_accounts
_save_client_accounts = _db._save_client_accounts
create_client_account = _db.create_client_account
delete_client_account = _db.delete_client_account
update_client_perms   = _db.update_client_perms
authenticate_client   = _db.authenticate_client

# ── Curadoria helpers — delegated to db.py ────────────────────────────────────
CURADORIA_PATH       = "data/curadoria.json"
load_curadoria       = _db.load_curadoria
_save_curadoria      = _db._save_curadoria
add_curadoria_item   = _db.add_curadoria_item
remove_curadoria_item = _db.remove_curadoria_item

# ── Project Folders — delegated to db.py ──────────────────────────────────────
PROJECT_FOLDERS_PATH  = "data/project_folders.json"
load_project_folders  = _db.load_project_folders
_save_project_folders = _db._save_project_folders
create_project_folder = _db.create_project_folder
delete_project_folder = _db.delete_project_folder

def add_url_current(url: str, user: str, folder_id: str, note: str = "") -> dict:
    """Fetch a URL, extract its key insight via Claude, and save it as a
    collected current in the given project folder.

    Returns the saved item dict, or raises on error.
    """
    import anthropic as _ant

    # 1. Fetch page HTML -------------------------------------------------------
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Lighthouse/1.0 compatible)"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw_html = r.read().decode("utf-8", errors="ignore")[:20000]

    # 2. Basic HTML → plain text strip ----------------------------------------
    text = _re_global.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=_re_global.DOTALL)
    text = _re_global.sub(r"<style[^>]*>.*?</style>", " ", text, flags=_re_global.DOTALL)
    text = _re_global.sub(r"<[^>]+>", " ", text)
    text = _re_global.sub(r"\s+", " ", text).strip()[:6000]

    # 3. Claude extraction -----------------------------------------------------
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    _ant_client = _ant.Anthropic(api_key=api_key)
    note_ctx = f"\nUser note: {note}" if note else ""
    extraction_prompt = f"""You are a cultural intelligence analyst. Extract the key insight from this
webpage as a current for a strategic intelligence platform. Be concise.{note_ctx}

URL: {url}
Page text:
{text}

Return ONLY valid JSON (no markdown fences):
{{
  "title": "6-12 word headline capturing the core insight",
  "summary": "2-3 sentences — what this signals culturally or competitively",
  "category": "cultural | competitive | social",
  "source_label": "publication or domain name"
}}"""
    msg = _ant_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        temperature=0.3,
        system="Extract webpage content as a structured current. Return only raw JSON.",
        messages=[{"role": "user", "content": extraction_prompt}],
    )
    data = _extract_json(msg.content[0].text)

    title   = data.get("title", url[:80])
    summary = data.get("summary", "")
    if note:
        summary = f"{summary}\n\n📎 Note: {note}"
    cat     = data.get("category", "cultural")
    src_lbl = data.get("source_label", urllib.parse.urlparse(url).netloc)

    # 4. Save to curadoria + assign to project folder -------------------------
    new_item = {
        "id":         str(uuid.uuid4())[:8],
        "user":       user,
        "type":       f"URL · {src_lbl}",
        "title":      title,
        "content":    summary,
        "url":        url,
        "category":   cat,
        "saved_at":   datetime.utcnow().strftime("%d %b %Y · %H:%M"),
        "folder_ids": [folder_id],
    }
    _db.add_url_current_to_curadoria(new_item)
    return new_item


set_item_folders = _db.set_item_folders

def _filter_items_by_active_folder(items: list) -> list:
    """Filter board items by the folder selected in the Project Folders bar."""
    active = st.session_state.get("active_folder", "all")
    if active == "all":
        return items
    if active == "unsorted":
        return [i for i in items if not i.get("folder_ids")]
    return [i for i in items if active in (i.get("folder_ids") or [])]

def _render_board_item(item: dict, folders: list, color: str = "#0a7d8c",
                        show_user_pill: bool = False, allow_delete: bool = False,
                        ctx: str = ""):
    """Renders a single board item as a card, with folder-assignment popover
    and optional delete button.

    `ctx` disambiguates widget keys when the same item is rendered in more
    than one place (e.g. My Board vs. Team Board)."""
    folder_map = {f["id"]: f["name"] for f in folders}
    item_folder_ids = [fid for fid in item.get("folder_ids", []) if fid in folder_map]
    folder_pills = "".join(
        f'<span class="cur-folder-pill">📁 {e(folder_map[fid])}</span>' for fid in item_folder_ids
    )
    user_pill = (
        f'<span class="cur-user-pill" style="background:{color}">{e(item["user"])}</span>'
        if show_user_pill else ""
    )

    col_a, col_b, col_c = st.columns([16, 1, 1])
    with col_a:
        st.markdown(f"""
<div class="cur-item" style="border-left-color:{color}">
  <div class="cur-item-type">{e(item['type'])}</div>
  <div class="cur-item-title">{e(item['title'][:120])}</div>
  <div class="cur-item-content">{e(item['content'][:240])}{"…" if len(item['content']) > 240 else ""}</div>
  <div class="cur-item-meta">
    {user_pill}{folder_pills}
    Saved on {e(item['saved_at'])}
  </div>
</div>""", unsafe_allow_html=True)
    with col_b:
        st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
        with st.popover("📁", use_container_width=True, help="Organize into project folders"):
            if not folders:
                st.caption("No project folders yet — create one above.")
            else:
                selected = st.multiselect(
                    "Folders", options=[f["id"] for f in folders],
                    default=item_folder_ids,
                    format_func=lambda fid: folder_map.get(fid, fid),
                    key=f"folders_{ctx}_{item['id']}", label_visibility="collapsed",
                )
                if st.button("Apply", key=f"apply_folders_{ctx}_{item['id']}", use_container_width=True):
                    set_item_folders(item["id"], selected)
                    st.rerun()
    with col_c:
        st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
        if allow_delete:
            if st.button("🗑", key=f"del_{ctx}_{item['id']}", help="Remove from board"):
                remove_curadoria_item(item["id"])
                st.rerun()


# ── Countercurrent overrides ────────────────────────────────────────────────────
# The AI-generated countercurrent is a *starting point*. The team can rewrite it
# manually per dispatch — the edited version then takes precedence over the draft.
COUNTERCURRENT_OVERRIDES_PATH       = "data/countercurrent_overrides.json"
load_countercurrent_overrides       = _db.load_countercurrent_overrides
save_countercurrent_override        = _db.save_countercurrent_override
clear_countercurrent_override       = _db.clear_countercurrent_override


# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="The Lighthouse",
    page_icon="🗼",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
#MainMenu, header, footer { visibility: hidden; }

/* ── Global CSS variable override — force light theme vars so Streamlit
   elements that inherit --background-color don't render dark-on-dark ── */
:root {
    --background-color:           #f0f4f7 !important;
    --secondary-background-color: #e4edf4 !important;
    --text-color:                 #071828 !important;
    --font:                       "Source Sans Pro", sans-serif;
}

/* ── code / pre blocks — Streamlit defaults these to a near-black bg ── */
[data-testid="stAppViewContainer"] pre,
[data-testid="stAppViewContainer"] code:not([class*="language"]) {
    background: #e8f0f6 !important;
    background-color: #e8f0f6 !important;
    color: #274d68 !important;
    border-radius: 4px !important;
}
[data-testid="stAppViewContainer"] .stCodeBlock,
[data-testid="stAppViewContainer"] [data-testid="stCode"] {
    background: #e8f0f6 !important;
    color: #274d68 !important;
}
[data-testid="stAppViewContainer"] [data-testid="stCode"] pre {
    background: #e8f0f6 !important;
    color: #274d68 !important;
}

/* ── board cards — force every descendant to stay on light bg ── */
.cur-item, .cur-item * {
    background: transparent !important;
    background-color: transparent !important;
}
.cur-item {
    background: #ffffff !important;
    background-color: #ffffff !important;
}
.cur-item-meta {
    background: transparent !important;
    color: #6ea8c4 !important;
}

/* ── stMarkdownContainer — prevent dark bg from bleeding through ── */
[data-testid="stMarkdownContainer"],
[data-testid="stVerticalBlock"] {
    background-color: transparent !important;
}

/* ── Atlantic background — garante o sea mist em todos os contextos ── */
[data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] > section,
.main {
    background-color: #ebf2f7 !important;
    background-image:
        radial-gradient(ellipse 80% 40% at 50% -10%, rgba(10,125,140,.07), transparent),
        radial-gradient(ellipse 60% 30% at 90% 110%, rgba(6,34,51,.04), transparent);
}
.block-container { padding: 64px 0.5rem 0 !important; max-width: 100% !important; background: transparent !important; }

/* ── Sticky top navigation bar ── */
.lh-topnav {
    position: fixed !important;
    top: 0 !important; left: 0 !important; right: 0 !important;
    height: 46px;
    z-index: 999991 !important;
    display: flex !important;
    align-items: center;
    padding: 0 28px;
    gap: 16px;
    background: rgba(6,34,51,.97) !important;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    box-shadow: 0 1px 24px rgba(0,0,0,.3);
    border-bottom: 1px solid rgba(10,125,140,.3);
}
.lh-topnav .lh-nav-logo {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10.5px; font-weight: 700;
    letter-spacing: .2em; text-transform: uppercase;
    color: #0fa3b5;
}
.lh-topnav .lh-nav-client {
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px; letter-spacing: .08em; text-transform: uppercase;
    color: rgba(208,234,240,.55);
}
.lh-topnav .lh-nav-sep {
    width: 1px; height: 16px;
    background: rgba(255,255,255,.15); flex: none;
}
.lh-topnav .lh-nav-user {
    margin-left: auto;
    width: 26px; height: 26px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-family: 'Georgia', serif; font-weight: 600;
    font-size: 11px; color: #fff; flex: none;
}

/* Prevent white text leaking into light sections */
[data-testid="stTabsContent"] p,
[data-testid="stTabsContent"] div,
[data-testid="stTabsContent"] span {
    color: #071828;
}
[data-testid="stTabsContent"] .cur-item-type  { color: #0a7d8c !important; }
[data-testid="stTabsContent"] .cur-item-title { color: #071828 !important; }
[data-testid="stTabsContent"] .cur-item-content { color: #274d68 !important; }
[data-testid="stTabsContent"] .cur-user-pill  { color: #fff !important; }
/* Streamlit default p/text in light mode */
p, li, span, label { color: #071828; }

section[data-testid="stSidebar"] { background: #0f0e0c !important; }
section[data-testid="stSidebar"] * { color: #c8c0b4 !important; }
section[data-testid="stSidebar"] .stButton > button {
    background: #1a1714 !important; border: 1px solid #3a3530 !important;
    color: #e8a838 !important; font-family: monospace !important;
    font-size: 0.72rem !important; text-transform: uppercase !important;
    letter-spacing: 0.08em !important; width: 100%;
}
section[data-testid="stSidebar"] .stButton > button:hover { border-color: #e8a838 !important; }
section[data-testid="stSidebar"] input, section[data-testid="stSidebar"] textarea {
    background: #1a1714 !important; border: 1px solid #2a2520 !important;
    color: #c8c0b4 !important; font-size: 0.82rem !important;
}
iframe { border: none !important; }

/* ── Expanders — force light background ── */
[data-testid="stExpander"] {
    background: #ffffff !important;
    border: 1px solid #9dc4d8 !important;
    border-radius: 6px !important;
}
[data-testid="stExpander"] summary {
    color: #071828 !important;
    background: transparent !important;
}
[data-testid="stExpander"] div[data-testid="stExpanderDetails"] {
    background: #ffffff !important;
    color: #274d68 !important;
}
[data-testid="stExpander"] p,
[data-testid="stExpander"] li,
[data-testid="stExpander"] td,
[data-testid="stExpander"] th,
[data-testid="stExpander"] code {
    color: #274d68 !important;
    background: transparent !important;
}
[data-testid="stExpander"] table {
    background: #fff !important;
    border-collapse: collapse;
}
[data-testid="stExpander"] th {
    background: #ebf2f7 !important;
    font-weight: 600 !important;
}

/* ── Main-area save/edit/delete buttons (✓ 🗂️ ✎ 🗑 💾 ↺) — transparent,
   beacon on hover. Sized up from the original 15px/28px so the icon-only
   controls (save-to-project, edit countercurrent, remove from board) read
   clearly at a glance instead of disappearing as tiny glyphs. */
/* button[kind] beats Streamlit's hashed Emotion class in specificity */
[data-testid="stAppViewContainer"] button[kind="secondary"] {
    background: transparent !important;
    background-color: transparent !important;
    border: 1px solid rgba(157,196,216,.45) !important;
    border-radius: 8px !important;
    color: #274d68 !important;
    box-shadow: none !important;
    font-size: 19px !important;
    padding: 6px 14px !important;
    min-height: 40px !important;
    min-width: 40px !important;
    transition: all .15s !important;
}
[data-testid="stAppViewContainer"] button[kind="secondary"]:hover {
    border-color: #0a7d8c !important;
    color: #0a7d8c !important;
    background: rgba(10,125,140,.08) !important;
    background-color: rgba(10,125,140,.08) !important;
}
[data-testid="stAppViewContainer"] button[kind="secondary"] p,
[data-testid="stAppViewContainer"] button[kind="secondary"] div {
    color: inherit !important;
    background: transparent !important;
    font-size: 19px !important;
    line-height: 1.3 !important;
}

/* ── Login form submit button ── */
[data-testid="stFormSubmitButton"] > button,
[data-testid="stFormSubmitButton"] > button:focus {
    background: #071828 !important;
    background-color: #071828 !important;
    color: #ffffff !important;
    border: 1px solid #071828 !important;
    border-radius: 6px !important;
    font-size: 15px !important;
    padding: 10px 20px !important;
    width: 100% !important;
    box-shadow: none !important;
    transition: background .15s !important;
}
[data-testid="stFormSubmitButton"] > button:hover {
    background: #0a7d8c !important;
    background-color: #0a7d8c !important;
    border-color: #0a7d8c !important;
    color: #ffffff !important;
}
[data-testid="stFormSubmitButton"] > button p,
[data-testid="stFormSubmitButton"] > button span {
    color: #ffffff !important;
}

/* ── Top-level navigation (Trends / Dispatch / Projects / Road Map) ──────────
   Editorial anchor-link style — clean white bar, underline indicator.
   Scoped via #lh-toptabs-marker so nested tab bars keep default look. */
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] [data-baseweb="tab-list"] {
    gap: 0;
    background: #ffffff;
    border-radius: 0;
    padding: 0 8px;
    margin: 0 0 0;
    border-bottom: 1px solid #e2e8ed;
    box-shadow: none;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] [data-baseweb="tab-list"] [data-baseweb="tab-border"] {
    display: none !important;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] [data-baseweb="tab-highlight"] {
    display: none !important;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] button[data-baseweb="tab"] {
    height: 44px;
    padding: 0 22px;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 10px !important;
    font-weight: 400 !important;
    letter-spacing: .18em;
    text-transform: uppercase;
    color: #8ba8bc !important;
    background: transparent !important;
    border-radius: 0 !important;
    border-bottom: 2px solid transparent;
    transition: color .15s, border-color .15s;
    box-shadow: none !important;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] button[data-baseweb="tab"] p {
    font-size: 10px !important;
    font-weight: 400 !important;
    letter-spacing: .18em;
    color: #8ba8bc !important;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {
    color: #071828 !important;
    background: transparent !important;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] button[data-baseweb="tab"]:hover p {
    color: #071828 !important;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] button[aria-selected="true"] {
    color: #071828 !important;
    background: transparent !important;
    border-bottom: 2px solid #071828 !important;
    box-shadow: none !important;
}
div[data-testid="stElementContainer"]:has(#lh-toptabs-marker)
  + div[data-testid="stTabs"] button[aria-selected="true"] p {
    color: #071828 !important;
    font-weight: 500 !important;
}

/* ── Popovers ("+ Add to project", folder picker, client access) ──────────
   st.popover renders its panel in a portal outside the themed app container,
   so it falls back to Streamlit's dark default — illegible against this
   app's light "sea mist" palette. The actual styled card is the inner
   [data-testid="stPopoverBody"] (a baseweb Popover.Body with its own
   inline-styled dark background), not the outer positioning
   div[data-baseweb="popover"] — so both need to be repainted, and every
   descendant's text color needs to be re-asserted (a direct rule on each
   element beats an inherited color, however !important the ancestor is). */
div[data-baseweb="popover"],
[data-testid="stPopoverBody"] {
    --background-color: #ffffff;
    --secondary-background-color: #ebf2f7;
    --text-color: #071828;
    background-color: #ffffff !important;
    color: #071828 !important;
    border: 1px solid #9dc4d8 !important;
    border-radius: 8px !important;
    box-shadow: 0 8px 28px rgba(7,24,40,.18) !important;
}
[data-testid="stPopoverBody"] *:not(button):not([data-baseweb="tag"]) {
    color: #071828 !important;
    background-color: transparent !important;
}
[data-testid="stPopoverBody"] [data-baseweb="tag"] {
    background-color: #ebf2f7 !important;
    color: #071828 !important;
}
/* "Add to project" / folder action buttons inside the popover — same
   transparent-with-border treatment as the main-area save buttons, since
   the [data-testid="stAppViewContainer"]-scoped rule above doesn't reach
   into this portal. */
[data-testid="stPopoverBody"] button[kind="secondary"] {
    background: #ffffff !important;
    background-color: #ffffff !important;
    border: 1px solid #9dc4d8 !important;
    color: #071828 !important;
    border-radius: 6px !important;
}
[data-testid="stPopoverBody"] button[kind="secondary"]:hover {
    border-color: #0a7d8c !important;
    color: #0a7d8c !important;
}
[data-testid="stPopoverBody"] button[kind="secondary"] p,
[data-testid="stPopoverBody"] button[kind="secondary"] div,
[data-testid="stPopoverBody"] button[kind="secondary"] span {
    color: inherit !important;
}
[data-testid="stPopoverBody"] button[kind="primary"] {
    background: #0a7d8c !important;
    background-color: #0a7d8c !important;
    border: 1px solid #0a7d8c !important;
    color: #ffffff !important;
}
[data-testid="stPopoverBody"] button[kind="primary"] p,
[data-testid="stPopoverBody"] button[kind="primary"] div,
[data-testid="stPopoverBody"] button[kind="primary"] span {
    color: #ffffff !important;
}

/* ── Tooltips (the "?" help text on buttons, inputs, etc.) ─────────────────
   Same dark-on-dark bug class as the popover above: stTooltipContent renders
   in a portal with Streamlit's dark default background, and the global
   `p, li, span, label { color:#071828 }` rule then paints its text near-black
   on top of that dark background. Repaint both the panel and its text. */
[data-testid="stTooltipContent"] {
    background-color: #062233 !important;
    color: #e8f6fa !important;
    border: 1px solid rgba(157,196,216,.35) !important;
    border-radius: 6px !important;
    box-shadow: 0 8px 28px rgba(7,24,40,.25) !important;
}
[data-testid="stTooltipContent"] * {
    color: #e8f6fa !important;
    background-color: transparent !important;
}
</style>
""", unsafe_allow_html=True)

# ── Dispatch archive helpers (defined early — called inside sidebar) ───────────

def load_all_dispatches(path: str = "data/dispatches.jsonl") -> list:
    """Load all saved dispatches, newest first. Delegates to db.py."""
    return _db.load_all_dispatches()


def dispatch_label(rec: dict) -> str:
    """Human-readable label for a dispatch record."""
    ts    = rec.get("timestamp", "")[:10]
    title = rec.get("full", {}).get("lead", {}).get("title", rec.get("content", ""))
    short = (title[:42] + "…") if len(title) > 42 else title
    return f"{ts}  ·  {short}" if short else ts


# ── Login screen ──────────────────────────────────────────────────────────────

def show_login():
    """Full-screen Atlantic-styled login. Blocks the rest of the app."""
    st.markdown("""
<style>
.login-wrap {
    max-width: 400px; margin: 6rem auto 0; padding: 2.5rem;
    background: #fff; border: 1px solid #9dc4d8; border-radius: 10px;
    box-shadow: 0 8px 32px rgba(7,24,40,.08);
}
.login-logo {
    text-align: center; margin-bottom: 1.8rem;
}
.login-logo .the {
    font-family: monospace; font-size: 10px; letter-spacing: .42em;
    text-transform: uppercase; color: #274d68; display: block; margin-bottom: 4px;
}
.login-logo h1 {
    font-family: Georgia, serif; font-size: 38px; font-weight: 600;
    color: #071828; margin: 0; letter-spacing: -.01em; line-height: 1;
}
.login-logo .tagline {
    font-family: Georgia, serif; font-style: italic;
    font-size: 13px; color: #274d68; margin-top: 6px;
}
.login-agency {
    text-align: center; font-family: monospace; font-size: 9px;
    letter-spacing: .16em; text-transform: uppercase; color: #0a7d8c;
    margin-bottom: 1.8rem;
}
/* Login form submit button */
[data-testid="stFormSubmitButton"] > button,
[data-testid="stFormSubmitButton"] > button:focus {
    background: #071828 !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 6px !important;
    font-family: Georgia, serif !important;
    font-size: 15px !important;
    font-weight: 500 !important;
    letter-spacing: .04em !important;
    padding: 12px 20px !important;
    width: 100% !important;
    cursor: pointer !important;
    transition: background .15s !important;
}
[data-testid="stFormSubmitButton"] > button:hover {
    background: #0a7d8c !important;
    color: #ffffff !important;
}
</style>
<div class="login-wrap">
  <div class="login-logo">
    <span class="the">The</span>
    <h1>Lighthouse</h1>
    <div class="tagline">Cultural Intelligence Platform</div>
  </div>
  <div class="login-agency">Atlantic · New York · Countercurrent.ai</div>
</div>
""", unsafe_allow_html=True)

    # Use st.columns to center the form under the HTML above
    _, col, _ = st.columns([1, 2, 1])
    with col:
        tab_team, tab_client = st.tabs(["Agency Team", "Client Access"])

        with tab_team:
            with st.form("login_form", clear_on_submit=False):
                user_sel = st.selectbox("Username", list(USERS.keys()), label_visibility="visible")
                password = st.text_input("Password", type="password", placeholder="••••••••")
                submitted = st.form_submit_button("Sign in →", use_container_width=True)
                if submitted:
                    if USERS.get(user_sel) == password:
                        st.session_state.logged_in_user = user_sel
                        st.session_state.user_role = "internal"
                        st.rerun()
                    else:
                        st.error("Incorrect password.")

        with tab_client:
            client_accounts = load_client_accounts()
            if not client_accounts:
                st.caption("No client accounts configured yet. Ask your agency contact for access.")
            else:
                with st.form("client_login_form", clear_on_submit=False):
                    cl_user_sel = st.selectbox(
                        "Client login", [a["username"] for a in client_accounts],
                        label_visibility="visible",
                    )
                    cl_password = st.text_input("Password", type="password",
                                                 placeholder="••••••••", key="cl_password")
                    cl_submitted = st.form_submit_button("Sign in →", use_container_width=True)
                    if cl_submitted:
                        acct = authenticate_client(cl_user_sel, cl_password)
                        if acct:
                            st.session_state.logged_in_user = acct["username"]
                            st.session_state.user_role = "client"
                            st.session_state.client_label = acct.get("client_label", acct["username"])
                            st.session_state.client_perms = acct.get("perms", {})
                            st.rerun()
                        else:
                            st.error("Incorrect password.")


if "logged_in_user" not in st.session_state:
    show_login()
    st.stop()

# ── Role / permissions for the current session ─────────────────────────────────
USER_ROLE     = st.session_state.get("user_role", "internal")
IS_CLIENT     = USER_ROLE == "client"
CLIENT_LABEL  = st.session_state.get("client_label", "")
CLIENT_PERMS  = st.session_state.get("client_perms", {})

def _has_perm(key: str) -> bool:
    """True for internal team members; for clients, checks their permission set."""
    if not IS_CLIENT:
        return True
    return bool(CLIENT_PERMS.get(key, False))

# ── Client brand config (from .env) ───────────────────────────────────────────
# Set these in .env to customise per client:
#   CLIENT_BEACON_COLOR = #cf2b29   (Heinz red, or any brand color)
#   CLIENT_BEACON_2     = #e0502f   (lighter shade of brand color)
#   CLIENT_PILL_COLOR   = #0a4a6e   (pill background — defaults to Atlantic blue)
#   AGENCY_NAME         = Atlantic · New York
CLIENT_BEACON_COLOR = os.environ.get("CLIENT_BEACON_COLOR", "#0a7d8c")   # default: teal
CLIENT_BEACON_2     = os.environ.get("CLIENT_BEACON_2",     "#0fa3b5")
CLIENT_PILL_COLOR   = os.environ.get("CLIENT_PILL_COLOR",   "#0a4a6e")
AGENCY_NAME         = os.environ.get("AGENCY_NAME",         "Atlantic · New York")

# ── Sidebar — config ───────────────────────────────────────────────────────────
with st.sidebar:
    current_user  = st.session_state.logged_in_user
    user_color    = USER_COLORS.get(current_user, "#0a7d8c")

    if IS_CLIENT:
        st.markdown(f"""
<div style="font-family:'Georgia',serif;font-size:20px;color:#d0eaf0;margin-bottom:4px">
  🗼 THE LIGHTHOUSE
</div>
<div style="font-family:monospace;font-size:10px;color:#0fa3b5;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:12px">
  Client View · {e(CLIENT_LABEL)}
</div>
<div style="display:flex;align-items:center;gap:10px;background:#1a2a3a;border-radius:6px;padding:8px 12px;margin-bottom:4px">
  <div style="width:28px;height:28px;border-radius:50%;background:{user_color};display:flex;align-items:center;justify-content:center;font-family:Georgia,serif;font-weight:600;font-size:12px;color:#fff;flex:none">{e(CLIENT_LABEL[:1].upper() or "C")}</div>
  <div>
    <div style="font-size:13px;color:#d0eaf0;font-weight:500">{e(CLIENT_LABEL)}</div>
    <div style="font-family:monospace;font-size:9px;color:#0a7d8c;letter-spacing:.08em;text-transform:uppercase">Client access</div>
  </div>
</div>
""", unsafe_allow_html=True)
    else:
        st.markdown(f"""
<div style="font-family:'Georgia',serif;font-size:20px;color:#d0eaf0;margin-bottom:4px">
  🗼 THE LIGHTHOUSE
</div>
<div style="font-family:monospace;font-size:10px;color:#0fa3b5;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:12px">
  Atlantic · Countercurrent.ai · v3
</div>
<div style="display:flex;align-items:center;gap:10px;background:#1a2a3a;border-radius:6px;padding:8px 12px;margin-bottom:4px">
  <div style="width:28px;height:28px;border-radius:50%;background:{user_color};display:flex;align-items:center;justify-content:center;font-family:Georgia,serif;font-weight:600;font-size:12px;color:#fff;flex:none">{current_user[0]}</div>
  <div>
    <div style="font-size:13px;color:#d0eaf0;font-weight:500">{current_user}</div>
    <div style="font-family:monospace;font-size:9px;color:#0a7d8c;letter-spacing:.08em;text-transform:uppercase">Online</div>
  </div>
</div>
""", unsafe_allow_html=True)

    if st.button("Sign out", use_container_width=True, key="logout_btn"):
        for _k in ("logged_in_user", "user_role", "client_label", "client_perms"):
            st.session_state.pop(_k, None)
        st.rerun()

    if IS_CLIENT:
        # Read-only client session — no editorial controls. Reasonable
        # defaults so the rest of the app (which expects these names) works.
        client_name     = CLIENT_LABEL or "Client"
        brief_tagline   = ""
        focus_topic     = ""
        client_filter   = ""
        competitors_raw = ""
        use_pinecone    = True
        signal_limit    = 20
        live_mode       = False
        regenerate      = False
        email_to        = ""
        send_email_btn  = False
        _has_content    = bool(st.session_state.get("lh_content"))
        _download_placeholder = st.empty()
        _date_str       = datetime.utcnow().strftime("%Y-%m-%d")

        st.markdown("---")
        st.caption("You're viewing a read-only client dispatch. Some sections may be hidden based on your access level.")
    else:
        client_name   = st.text_input("Client", value="Heinz Soup · United Kingdom")
        brief_tagline = st.text_input("Brief tagline", value="Reading Britain's lunch currents so Heinz can build the countercurrent.")
        focus_topic   = st.text_area(
            "Focus topic / brief",
            value="desk lunch, comfort food, cost of living, office return-to-work culture, UK workers",
            height=80,
        )
        client_filter   = st.text_input("Client tag filter", value="", placeholder="Leave blank = all signals")
        competitors_raw = st.text_input(
            "Competitor brands",
            value="Cully & Sully, New Covent Garden, Batchelors, Cup-a-Soup",
            help="Comma-separated — used in Competitive Pulse",
        )

        st.markdown("---")
        use_pinecone  = st.checkbox("Use Pinecone semantic search", value=True)
        signal_limit  = st.slider("Signals to analyse", 10, 50, 20)

        st.markdown("---")
        st.markdown(
            '<div style="font-size:10px;color:#0fa3b5;font-family:monospace;'
            'text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px">'
            'Mode</div>',
            unsafe_allow_html=True,
        )
        live_mode = st.toggle("Live — call Claude", value=False,
                              help="OFF = shows last saved dispatch (no cost).\nON = generates new content via Claude.")
        regenerate = st.button("⚡  Sweep & Generate", use_container_width=True,
                               disabled=not live_mode)
        _sig_count_placeholder = st.empty()   # filled after load_signals() is defined below

        st.markdown("---")
        st.markdown(
            '<div style="font-size:10px;color:#555;font-family:monospace;'
            'text-transform:uppercase;letter-spacing:0.08em">Email Dispatch</div>',
            unsafe_allow_html=True,
        )
        email_to      = st.text_input("Send to", placeholder="strategist@agency.com", label_visibility="collapsed")
        send_email_btn = st.button("Send via SendGrid", use_container_width=True)

        # ── PDF / HTML Report download ──────────────────────────────────────
        st.markdown("---")
        st.markdown(
            '<div style="font-size:10px;color:#0fa3b5;font-family:monospace;'
            'text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px">'
            '↓ Report</div>',
            unsafe_allow_html=True,
        )
        # Download button placeholder — filled after build_html is defined
        _has_content = bool(st.session_state.get("lh_content"))
        _download_placeholder = st.empty()
        _date_str = datetime.utcnow().strftime("%Y-%m-%d")
        if not _has_content:
            _download_placeholder.caption("Generate a dispatch first to download the report.")

        # ── Dispatch archive ──────────────────────────────────────────────────
        st.markdown("---")
        st.markdown(
            '<div style="font-size:10px;color:#0fa3b5;font-family:monospace;'
            'text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px">'
            '◷ Dispatch Archive</div>',
            unsafe_allow_html=True,
        )
        _all_dispatches = load_all_dispatches()
        if _all_dispatches:
            _archive_labels = ["— current dispatch —"] + [dispatch_label(r) for r in _all_dispatches]
            _sel = st.selectbox("Browse history", _archive_labels, label_visibility="collapsed")
            if _sel != "— current dispatch —":
                _idx = _archive_labels.index(_sel) - 1
                if st.button("Load this dispatch", use_container_width=True):
                    st.session_state.lh_content = _all_dispatches[_idx]["full"]
                    st.rerun()
        else:
            st.caption("No archived dispatches yet.")

        st.markdown("---")

        # ── In-app ingestion panel ──────────────────────────────────────────
        with st.expander("📡 Run Ingestion", expanded=False):
            st.caption("Populate the signal database directly from Streamlit Cloud.")
            _ing_topic = st.text_area(
                "Topic / brief",
                value=focus_topic or "cultural trends, consumer behaviour",
                height=68,
                key="ing_topic",
            )
            _ing_client = st.text_input(
                "Client tag",
                value=client_name.replace(" ", "_")[:20] if client_name else "",
                key="ing_client_tag",
            )
            _ing_limit = st.slider("Max signals", 20, 200, 100, key="ing_limit")
            _ing_sources = st.multiselect(
                "Sources",
                ["Reddit", "RSS", "GDELT", "Google Trends", "Hacker News", "Exa",
                 "YouTube", "TikTok", "Instagram", "X/Twitter"],
                default=["Reddit", "RSS", "GDELT", "Google Trends", "Hacker News"],
                key="ing_sources",
            )
            _ing_geo = st.selectbox(
                "Google Trends geography",
                ["🌍 Worldwide", "🇬🇧 UK", "🇺🇸 USA", "🇧🇷 Brazil"],
                key="ing_geo",
            ) if "Google Trends" in (_ing_sources if "ing_sources" in st.session_state else []) else "🌍 Worldwide"
            _geo_map = {"🌍 Worldwide": "", "🇬🇧 UK": "GB", "🇺🇸 USA": "US", "🇧🇷 Brazil": "BR"}
            _geo_code = _geo_map.get(_ing_geo, "")

            # Key warnings — show before running so user knows what will be skipped
            _ing_needs_apify  = {"TikTok", "Instagram", "X/Twitter"} & set(_ing_sources)
            _ing_needs_youtube = "YouTube" in _ing_sources
            _ing_has_apify    = bool(os.environ.get("APIFY_API_TOKEN", ""))
            _ing_has_youtube  = bool(os.environ.get("YOUTUBE_API_KEY", ""))
            if _ing_needs_apify and not _ing_has_apify:
                st.warning("⚠️ APIFY_API_TOKEN not set — TikTok / Instagram / X/Twitter will be skipped.")
            if _ing_needs_youtube and not _ing_has_youtube:
                st.warning("⚠️ YOUTUBE_API_KEY not set — YouTube will be skipped. Add it to Streamlit Cloud secrets.")

            if st.button("⚡ Start ingestion", use_container_width=True, key="run_ingestion_btn"):
                _ing_log = st.empty()
                _ing_lines: list[str] = []
                def _ing_cb(msg: str):
                    _ing_lines.append(msg)
                    _ing_log.code("\n".join(_ing_lines[-14:]))
                try:
                    from ingestion import run_ingestion as _run_ing
                    with st.spinner("🗼 Sweeping the web…"):
                        _result = _run_ing(
                            topic=_ing_topic,
                            client_tag=_ing_client or None,
                            limit=_ing_limit,
                            use_reddit="Reddit" in _ing_sources,
                            use_rss="RSS" in _ing_sources,
                            use_gdelt="GDELT" in _ing_sources,
                            use_google_trends="Google Trends" in _ing_sources,
                            use_hacker_news="Hacker News" in _ing_sources,
                            use_exa="Exa" in _ing_sources,
                            use_youtube="YouTube" in _ing_sources,
                            use_tiktok="TikTok" in _ing_sources,
                            use_instagram="Instagram" in _ing_sources,
                            use_twitter="X/Twitter" in _ing_sources,
                            trends_geo=_geo_code,
                            callback=_ing_cb,
                        )
                    st.success(f"✅ {_result['total']} signals saved — " + " · ".join(f"{k}: {v}" for k, v in _result["by_source"].items()))
                    st.cache_data.clear()
                    # Clear cached gallery + board so Trends tab rebuilds with fresh data
                    for _k in ("tr_gallery", "tr_board", "tr_openings", "tr_overview",
                               "tr_hunch_suggestions", "tr_topic_used", "tr_terms_used"):
                        st.session_state.pop(_k, None)
                    st.rerun()
                except Exception as _ing_exc:
                    st.error(f"Ingestion failed: {_ing_exc}")

        st.markdown(
            '<div style="font-size:10px;color:#444;font-family:monospace">'
            'Claude · Gemini Embeddings · Pinecone · Apify</div>',
            unsafe_allow_html=True,
        )


# ── Sticky top navigation bar ─────────────────────────────────────────────────
# Injected directly into document.body via window.parent — bypasses Streamlit's
# overflow:auto containers which prevent position:fixed from working.

_nav_client  = e(client_name.split("·")[0].strip())
_nav_user    = st.session_state.get("logged_in_user", "")
_nav_color   = USER_COLORS.get(_nav_user, "#0a7d8c")
_nav_initial = _nav_user[0] if _nav_user else "?"

# Section nav items: (label, anchor-id, visibility rule)
#   "always"   → shown to everyone
#   "internal" → hidden from clients
#   <perm key> → shown to internal users, or to clients with that permission
# NOTE: these anchors all live inside the "Dispatches" tab (see top-level
# st.tabs() below). Signal Lab and Vision Map are now their own top-level tabs
# ("Search" / "Roadmap") and are reached via the tab bar, not this anchor nav.
_NAV_ITEMS_ALL = [
    ("Lead Current",      "lh-sec-lead",        "always"),
    ("Countercurrent",    "lh-sec-cc",          "always"),
    ("Currents",          "lh-sec-currents",    "always"),
    ("Voices",            "lh-sec-voices",      "always"),
    ("Provocations",      "lh-sec-provs",       "always"),
    ("Topic Map",         "lh-sec-topicmap",    "topic_map"),
    ("Momentum",          "lh-sec-momentum",    "momentum"),
    ("Signal Volume",     "lh-sec-volume",      "signal_volume"),
    ("Competitive Pulse", "lh-sec-competitive", "competitive_pulse"),
]

def _nav_item_visible(rule: str) -> bool:
    if rule == "always":
        return True
    if rule == "internal":
        return not IS_CLIENT
    return _has_perm(rule)

_nav_items_filtered = [(label, anchor) for label, anchor, rule in _NAV_ITEMS_ALL if _nav_item_visible(rule)]
_nav_items_js = ",\n      ".join(
    "['%s', '%s']" % (label.replace("'", "\\'"), anchor) for label, anchor in _nav_items_filtered
)

st.components.v1.html(f"""
<script>
(function() {{
  try {{
    var p = window.parent.document;

    // Remove previous instance on re-render
    var old = p.getElementById('lh-topnav');
    if (old) old.remove();
    var oldStyle = p.getElementById('lh-topnav-style');
    if (oldStyle) oldStyle.remove();

    // Inject CSS into parent <head>
    var style = p.createElement('style');
    style.id = 'lh-topnav-style';
    style.textContent = `
      #lh-topnav {{
        position: fixed;
        top: 0; left: 0; right: 0;
        height: 46px;
        z-index: 999999;
        display: flex;
        align-items: center;
        padding: 0 16px 0 12px;
        gap: 10px;
        background: rgba(6,34,51,.97);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        box-shadow: 0 1px 20px rgba(0,0,0,.3);
        border-bottom: 1px solid rgba(10,125,140,.28);
        font-family: 'JetBrains Mono', monospace;
        transition: background .25s;
      }}
      #lh-topnav .lh-logo {{
        font-size: 10px; font-weight: 700;
        letter-spacing: .18em; text-transform: uppercase;
        color: #0fa3b5; white-space: nowrap; flex-shrink: 0;
      }}
      #lh-topnav .lh-sep {{
        width: 1px; height: 16px;
        background: rgba(255,255,255,.15); flex-shrink: 0;
      }}
      #lh-topnav .lh-client {{
        font-size: 8.5px; letter-spacing: .08em;
        text-transform: uppercase; color: rgba(208,234,240,.45);
        white-space: nowrap; flex-shrink: 0;
      }}
      #lh-topnav .lh-nav-links {{
        display: flex;
        align-items: center;
        gap: 1px;
        flex: 1;
        overflow-x: auto;
        scrollbar-width: none;
        -ms-overflow-style: none;
        padding: 0 4px;
      }}
      #lh-topnav .lh-nav-links::-webkit-scrollbar {{ display: none; }}
      #lh-topnav .lh-nav-link {{
        font-family: 'JetBrains Mono', monospace;
        font-size: 8px;
        letter-spacing: .07em;
        text-transform: uppercase;
        color: rgba(208,234,240,.45);
        padding: 4px 7px;
        border-radius: 3px;
        cursor: pointer;
        border: none;
        background: transparent;
        white-space: nowrap;
        transition: color .15s, background .15s;
        line-height: 1;
      }}
      #lh-topnav .lh-nav-link:hover {{
        color: #0fa3b5;
        background: rgba(10,125,140,.18);
      }}
      #lh-topnav .lh-user {{
        width: 26px; height: 26px; border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-family: Georgia, serif; font-weight: 600;
        font-size: 11px; color: #fff; flex-shrink: 0;
        background: {_nav_color};
      }}
      #lh-topnav .lh-menu-btn {{
        background: transparent;
        border: none;
        cursor: pointer;
        padding: 4px 6px;
        border-radius: 4px;
        color: rgba(208,234,240,.6);
        font-size: 16px;
        line-height: 1;
        transition: color .15s, background .15s;
        flex-shrink: 0;
      }}
      #lh-topnav .lh-menu-btn:hover {{
        color: #0fa3b5;
        background: rgba(10,125,140,.15);
      }}
      /* offset for sticky nav so anchors don't hide under it */
      [id^="lh-sec-"] {{
        scroll-margin-top: 54px;
      }}
    `;
    p.head.appendChild(style);

    // Sidebar toggle function — tries all known Streamlit selectors
    function toggleSidebar() {{
      var btn =
        p.querySelector('[data-testid="stSidebarCollapsedControl"] button') ||
        p.querySelector('[data-testid="stSidebarNavCollapseIcon"]') ||
        p.querySelector('header button[aria-label]') ||
        p.querySelector('[data-testid="collapsedControl"] button') ||
        p.querySelector('header button:first-of-type');
      if (btn) btn.click();
    }}

    // Section nav items: [label, anchor-id] — filtered server-side by role/permissions
    var NAV_ITEMS = [
      {_nav_items_js}
    ];

    var navLinksHtml = NAV_ITEMS.map(function(item) {{
      return '<button class="lh-nav-link" data-target="' + item[1] + '">' + item[0] + '</button>';
    }}).join('');

    // Inject nav bar into parent <body>
    var nav = p.createElement('div');
    nav.id = 'lh-topnav';
    nav.innerHTML =
      '<button class="lh-menu-btn" id="lh-menu-btn" title="Toggle sidebar">&#9776;</button>' +
      '<span class="lh-sep"></span>' +
      '<span class="lh-logo">🗼 Lighthouse</span>' +
      '<span class="lh-sep"></span>' +
      '<span class="lh-client">{_nav_client}</span>' +
      '<span class="lh-sep"></span>' +
      '<div class="lh-nav-links">' + navLinksHtml + '</div>' +
      '<div class="lh-user" title="{_nav_user}">{_nav_initial}</div>';
    p.body.appendChild(nav);

    // Attach sidebar toggle
    p.getElementById('lh-menu-btn').addEventListener('click', toggleSidebar);

    // Attach scroll-to for each nav link
    p.querySelectorAll('.lh-nav-link').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        var targetId = btn.getAttribute('data-target');
        var el = p.getElementById(targetId);
        if (el) el.scrollIntoView({{behavior: 'smooth', block: 'start'}});
      }});
    }});

  }} catch(err) {{ console.warn('Lighthouse topnav:', err); }}
}})();
</script>
""", height=0)



# ── Data loaders ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_signals(path: str = "data/signals.jsonl", limit: int = 200) -> list:
    """Load signals — from Supabase when configured, else from file."""
    return _db.load_signals(limit=limit)


def semantic_search(query: str, top_k: int = 15, client_filter: Optional[str] = None) -> list:
    """Query Pinecone via existing vectorizer (Gemini embeddings — unchanged)."""
    try:
        from vectorizer import query_knowledge_base
        filter_meta = {"client_tag": client_filter} if client_filter else None
        return query_knowledge_base(query, top_k=top_k, filter_metadata=filter_meta)
    except ImportError:
        return []
    except Exception as exc:
        st.warning(f"Semantic search unavailable: {exc}")
        return []


def build_context(signals: list, rag_results: list, limit: int = 25) -> str:
    parts = []
    for r in rag_results[:10]:
        parts.append(
            f"SIGNAL [RAG · relevance {r['score']:.2f}] [{r.get('source','?').upper()}]\n"
            f"{r['text'][:450]}"
        )
    seen = {r["text"][:80] for r in rag_results}
    for s in signals[:limit]:
        snippet = f"{s.get('title','')}::{s.get('content','')}"
        if snippet[:80] in seen:
            continue
        seen.add(snippet[:80])
        parts.append(
            f"SIGNAL [{s.get('source','?').upper()}] {s.get('timestamp','')[:10]}\n"
            f"URL: {s.get('url','')}\n"
            f"Title: {s.get('title','')}\n"
            f"Content: {s.get('content','')[:320]}"
        )
    return "\n\n---\n\n".join(parts[:25])


def save_dispatch(content: dict, topic: str):
    """Save a dispatch. Delegates to db.py (Supabase or file fallback)."""
    _db.save_dispatch(content, topic)


# ── Claude model config ────────────────────────────────────────────────────────
# Switch model here based on your phase:
#   Testing  → "claude-haiku-4-5-20251001"  (~$0.014/call, ~350 calls per $5)
#   Staging  → "claude-sonnet-4-6"           (~$0.042/call, ~120 calls per $5)
#   Client   → "claude-opus-4-5"             (~$0.070/call,  ~70 calls per $5)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ── Claude generation ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are The Lighthouse — an elite cultural intelligence engine for advertising strategy teams.

You analyse raw social signals (Reddit, TikTok, RSS, web) and surface strategic intelligence for a brand. Your output feeds a beautiful editorial dashboard that strategists and clients read every morning.

Your writing is sharp, editorial, specific. You think like a senior strategist at a world-class agency: you don't describe trends, you interrogate them. Your pull quotes feel real. Your provocations make people uncomfortable in a productive way.

You return ONLY valid JSON — no markdown fences, no explanation, no preamble. Just the raw JSON object.
"""


def generate(signals: list, rag: list, client: str, tagline: str, topic: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY not found. Add it to .env or Streamlit Secrets.")
        return _fallback()

    try:
        import anthropic as _anthropic
        claude = _anthropic.Anthropic(api_key=api_key)
    except ImportError:
        st.error("anthropic package not installed. Run: pip install anthropic")
        return _fallback()

    context = build_context(signals, rag)
    today   = datetime.utcnow().strftime("%A, %d %B %Y")
    sources = sorted({s.get("source", "?") for s in signals})

    prompt = f"""Client: {client}
Brief: {tagline}
Focus: {topic}
Date: {today}
Signal database: {len(signals)} signals across {', '.join(sources[:8])}

SIGNALS:
{context}

Generate a complete Lighthouse briefing. Return a single JSON object with EXACTLY this shape (no markdown, no fences, raw JSON only):

{{
  "sweep": {{
    "currents_surfaced": <integer>,
    "rising_fast": <integer>,
    "needs_human": <integer>
  }},
  "lead": {{
    "topic_tags": ["tag1", "tag2"],
    "relevance": "Direct | Adjacent | Peripheral",
    "momentum_pct": "+XXX%",
    "momentum_period": "7d",
    "momentum_dir": "up | down | flat",
    "title": "Lead headline — punchy, editorial, max 15 words",
    "dek": "2-3 sentences. Specific cultural tensions. References actual signal content.",
    "pullquote": "Vivid composite quote, 1-2 sentences, written as if by a real person online.",
    "pullquote_cite": "Platform · Community · engagement metric",
    "signal_stack": [
      {{"platform": "Name", "text": "What this signal shows", "num": "Metric"}},
      {{"platform": "Name", "text": "...", "num": "..."}},
      {{"platform": "Name", "text": "...", "num": "..."}},
      {{"platform": "Name", "text": "...", "num": "..."}}
    ],
    "countercurrent_title": "One-sentence strategic directive. Imperative, bold.",
    "countercurrent_body": "2-3 sentences. Specific timing, tactics, formats. Practical."
  }},
  "cards": [
    {{
      "momentum_pct": "+XX%",
      "momentum_dir": "up | down | flat",
      "category": "competitive | cultural | social",
      "tags": "Category · Signal type",
      "title": "Card headline — max 12 words",
      "body": "2 sentences. Specific to signals.",
      "sources": "Platform · Platform",
      "reach": "X.XM reach",
      "spark": [30, 40, 55, 65, 75, 85, 95]
    }},
    {{"momentum_pct": "+XX%", "momentum_dir": "up", "category": "competitive", "tags": "...", "title": "A rival brand's move worth tracking", "body": "...", "sources": "...", "reach": "...", "spark": [20, 35, 45, 55, 65, 75, 88]}},
    {{"momentum_pct": "+XX%", "momentum_dir": "up", "category": "cultural", "tags": "...", "title": "...", "body": "...", "sources": "...", "reach": "...", "spark": [25, 38, 48, 58, 68, 78, 90]}},
    {{"momentum_pct": "+XX%", "momentum_dir": "up", "category": "cultural", "tags": "...", "title": "...", "body": "...", "sources": "...", "reach": "...", "spark": [22, 34, 46, 58, 66, 76, 86]}},
    {{"momentum_pct": "+XX%", "momentum_dir": "up", "category": "social", "tags": "...", "title": "A platform-native trend or meme gaining steam", "body": "...", "sources": "...", "reach": "...", "spark": [18, 30, 42, 54, 66, 78, 92]}},
    {{"momentum_pct": "-XX%", "momentum_dir": "down", "category": "social", "tags": "Format War · Watch", "title": "A declining signal worth watching", "body": "...", "sources": "...", "reach": "...", "spark": [90, 82, 70, 60, 52, 44, 38]}}
  ],
  "voices": [
    {{"platform_class": "p-reddit", "platform_label": "Reddit · r/SubName", "engagement": "▲ 3.1k", "quote": "Vivid, specific, casual voice.", "handle": "u/realistic_handle", "rel_tag": "Short tag", "url": "https://reddit.com/r/SubName/comments/..."}},
    {{"platform_class": "p-tiktok", "platform_label": "TikTok", "engagement": "410k views", "quote": "...", "handle": "@handle", "rel_tag": "...", "url": "https://tiktok.com/@handle/video/..."}},
    {{"platform_class": "p-x", "platform_label": "X", "engagement": "12k likes", "quote": "...", "handle": "@handle", "rel_tag": "...", "url": ""}},
    {{"platform_class": "p-mumsnet", "platform_label": "Mumsnet", "engagement": "240 replies", "quote": "...", "handle": "Username", "rel_tag": "...", "url": ""}},
    {{"platform_class": "p-ig", "platform_label": "Instagram", "engagement": "22k likes", "quote": "...", "handle": "@handle", "rel_tag": "...", "url": ""}},
    {{"platform_class": "p-reddit", "platform_label": "Reddit · r/SubName", "engagement": "▲ 5.4k", "quote": "...", "handle": "u/handle", "rel_tag": "...", "url": "https://reddit.com/r/SubName/comments/..."}},
    {{"platform_class": "p-tiktok", "platform_label": "TikTok · Niche", "engagement": "880k views", "quote": "...", "handle": "@handle", "rel_tag": "...", "url": "https://tiktok.com/@handle/video/..."}},
    {{"platform_class": "p-x", "platform_label": "X", "engagement": "8.3k likes", "quote": "...", "handle": "@handle", "rel_tag": "...", "url": ""}},
    {{"platform_class": "p-reddit", "platform_label": "Reddit · r/SubName", "engagement": "▲ 1.9k", "quote": "...", "handle": "u/handle", "rel_tag": "...", "url": "https://reddit.com/r/SubName/comments/..."}}
  ],
  "provocations": [
    {{"n": "01", "text": "Open strategic question, 20-30 words, makes a strategist uncomfortable.", "tag": "Short philosophical tag"}},
    {{"n": "02", "text": "...", "tag": "..."}},
    {{"n": "03", "text": "...", "tag": "..."}}
  ],
  "briefing": "The 07:00 briefing. 2-3 sentences. Chief strategist speaking to the room. Ends with a specific call to action.",
  "alerts": [
    {{"sev": "hi", "text": "<b>Short bold phrase</b> brief explanation", "time": "4h ago · Competitor threat"}},
    {{"sev": "mid", "text": "<b>Short bold phrase</b> brief explanation", "time": "42 min ago · Opportunity"}},
    {{"sev": "lo", "text": "<b>Short bold phrase</b> brief explanation", "time": "2h ago · Opportunity"}}
  ]
}}

Be specific. Steal language from the actual signals. Write like a world-class strategist.
For "cards": provide 4-6 items covering all three lenses — at least one "competitive" (a rival
brand's move), at least one "cultural" (a broader shift in the conversation), and at least one
"social" (platform-native chatter, memes, trends). Set each card's "category" field accordingly.
For the "url" field in each voice: use the exact URL from the SIGNAL context above that best matches the quote. If no URL is available for that platform, leave it as an empty string ""."""

    try:
        msg = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            temperature=0.75,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip accidental markdown fences
        if "```" in raw:
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            raw   = raw[start:end]
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        st.warning(f"JSON parse error: {exc}. Using fallback.")
        return _fallback()
    except Exception as exc:
        st.error(f"Claude error: {exc}")
        return _fallback()


def _fallback() -> dict:
    return {
        "_dispatch_id": "fallback",
        "sweep": {"currents_surfaced": 0, "rising_fast": 0, "needs_human": 0},
        "lead": {
            "topic_tags": ["No data yet"],
            "relevance": "—",
            "momentum_pct": "—",
            "momentum_period": "—",
            "momentum_dir": "flat",
            "title": "No signals found — run ingestion first",
            "dek": (
                "The Lighthouse needs data. Run python NYLIBERTYingestion.py (or ingestion.py) "
                "to populate signals.jsonl, then switch to Live mode and generate."
            ),
            "pullquote": "The signal database is empty.",
            "pullquote_cite": "— System",
            "signal_stack": [
                {"platform": "System", "text": "No signals in database. Run ingestion.", "num": "0"}
            ],
            "countercurrent_title": "Run ingestion.py to populate the signal database.",
            "countercurrent_body": (
                "Once you have signals, The Lighthouse will generate a full strategic briefing automatically."
            ),
        },
        "cards": [],
        "voices": [],
        "provocations": [
            {"n": "01", "text": "What would you do if you had real data here?", "tag": "Run ingestion to find out"},
            {"n": "02", "text": "The countercurrent is hiding somewhere in the internet right now.", "tag": "Go get it"},
            {"n": "03", "text": "A brand that moves before the current peaks always looks like a genius in hindsight.", "tag": "Timing is everything"},
        ],
        "briefing": "No dispatch saved yet. Run ingestion.py, switch to Live mode, and generate.",
        "alerts": [{"sev": "mid", "text": "<b>No data</b> — run ingestion.py first", "time": "Now · System"}],
    }


# ── SendGrid email ─────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, html_body: str) -> bool:
    import urllib.request
    api_key   = os.environ.get("SENDGRID_API_KEY")
    from_addr = os.environ.get("SENDGRID_FROM_EMAIL", "dispatch@countercurrent.ai")
    if not api_key:
        return False
    payload = json.dumps({
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": from_addr},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }).encode()
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status == 202
    except Exception:
        return False


# ── HTML helpers ───────────────────────────────────────────────────────────────
# (e() is defined earlier, before the topnav block)

def _mclass(d: str) -> str:
    return {"up": "up", "down": "down"}.get(d, "flat")

def _marrow(d: str) -> str:
    return {"up": "▲", "down": "▼"}.get(d, "●")

def render_spark(values) -> str:
    bars = "".join(f'<i style="height:{v}%"></i>' for v in (values or [30, 40, 50, 60, 70, 80, 90]))
    return f'<div class="spark">{bars}</div>'

def render_signal_stack(stack: list) -> str:
    items = "".join(
        f'<div class="signal">'
        f'<span class="plat">{e(s.get("platform",""))}</span>'
        f'<span class="txt">{e(s.get("text",""))}</span>'
        f'<span class="num">{e(s.get("num",""))}</span>'
        f'</div>'
        for s in (stack or [])
    )
    return f'<div class="signal-stack">{items}</div>'

def render_card(c: dict) -> str:
    d = c.get("momentum_dir", "up")
    return (
        f'<article class="card">'
        f'<div class="ctop">'
        f'<span class="momentum {_mclass(d)}">{_marrow(d)} {e(c.get("momentum_pct",""))}</span>'
        f'<span class="brands">{e(c.get("tags",""))}</span>'
        f'</div>'
        f'<h3>{e(c.get("title",""))}</h3>'
        f'<p>{e(c.get("body",""))}</p>'
        f'{render_spark(c.get("spark"))}'
        f'<div class="card-foot">'
        f'<span>{e(c.get("sources",""))}</span>'
        f'<span class="reach">{e(c.get("reach",""))}</span>'
        f'</div></article>'
    )

def render_voice(v: dict) -> str:
    url     = v.get("url", "")
    link    = (f'<a href="{e(url)}" target="_blank" rel="noopener" '
               f'style="margin-left:auto;font-size:10px;color:var(--beacon);'
               f'text-decoration:none;font-family:\'JetBrains Mono\',monospace;'
               f'letter-spacing:.04em;">↗ source</a>') if url else ""
    return (
        f'<div class="voice {e(v.get("platform_class","p-reddit"))}">'
        f'<div class="vtop">'
        f'<span class="plat">● {e(v.get("platform_label",""))}</span>'
        f'<span class="eng">{e(v.get("engagement",""))}</span>'
        f'</div>'
        f'<div class="q">&ldquo;{e(v.get("quote",""))}&rdquo;</div>'
        f'<div class="vbot">'
        f'<span class="handle">{e(v.get("handle",""))}</span>'
        f'<span class="rel">{e(v.get("rel_tag",""))}</span>'
        f'{link}'
        f'</div></div>'
    )

def render_alert(a: dict) -> str:
    # alert text may include <b> tags — intentionally NOT escaped
    return (
        f'<div class="alert">'
        f'<div class="sev {e(a.get("sev","mid"))}"></div>'
        f'<div><div class="atxt">{a.get("text","")}</div>'
        f'<div class="atime">{e(a.get("time",""))}</div></div>'
        f'</div>'
    )

def render_prov(p: dict) -> str:
    return (
        f'<div class="prov">'
        f'<span class="n">{e(p.get("n",""))}</span>'
        f'<p>{e(p.get("text",""))}</p>'
        f'<span class="tag">{e(p.get("tag",""))}</span>'
        f'</div>'
    )

_ALL_KNOWN_SOURCES = [
    ("tiktok",   "TikTok"),
    ("instagram","Instagram"),
    ("twitter",  "X/Twitter"),
    ("youtube",  "YouTube"),
    ("reddit",   "Reddit"),
    ("rss",      "RSS"),
    ("gdelt",    "GDELT"),
    ("hn",       "HN"),
    ("web",      "Web"),
    ("trends",   "Trends"),
]

def sources_pills(signals: list) -> str:
    active = {s.get("source", "").lower() for s in signals}
    html = ""
    for key, label in _ALL_KNOWN_SOURCES:
        if key in active:
            html += f'<span class="src on"><span class="d"></span>{e(label)}</span>'
        else:
            html += f'<span class="src">{e(label)}</span>'
    return html

def chip_buttons(lead: dict) -> str:
    return "".join(
        f'<button class="chip">{e(t)}</button>'
        for t in lead.get("topic_tags", [])[:4]
    )

def _tr_proxy_thumb(url: str) -> str:
    """Route TikTok/Instagram CDN URLs through wsrv.nl to bypass hotlink protection.
    YouTube and other open CDNs are returned as-is."""
    if not url:
        return ""
    _protected = ("tiktokcdn.com", "tiktok.com", "cdninstagram.com",
                  "fbcdn.net", "instagram.com", "scontent")
    if any(d in url for d in _protected):
        return f"https://wsrv.nl/?url={urllib.parse.quote(url, safe='')}&n=-1&w=480"
    return url


# ── Full HTML renderer ─────────────────────────────────────────────────────────
# NOTE: All CSS curly braces are doubled ({{ }}) because this is an f-string.
#       Only {python_variable} expressions remain as single braces.

# ── Native CSS injected into Streamlit for interactive sections ────────────────

def _native_css(beacon: str, beacon_2: str) -> str:
    return f"""
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,400..700&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet"/>
<style>
:root{{
  --lh-ink:#071828; --lh-ink-soft:#274d68; --lh-paper:#ebf2f7;
  --lh-beacon:{beacon}; --lh-beacon-2:{beacon_2};
  --lh-line:#9dc4d8; --lh-line-strong:#6ea8c4;
  --lh-deep:#062233; --lh-rising:#1a8a6b; --lh-falling:#c94f35;
}}
.lh-eyebrow{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:{beacon};font-weight:700;}}
.lh-section-rule{{border-top:2px solid var(--lh-ink);padding-top:18px;margin-bottom:6px;}}
.lh-meta{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-family:'JetBrains Mono',monospace;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--lh-ink-soft);margin-bottom:12px;}}
.lh-tag{{background:{beacon};color:#fff;padding:3px 9px;font-weight:700;border-radius:3px;}}
.lh-momentum-up{{color:var(--lh-rising);font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;}}
.lh-momentum-down{{color:var(--lh-falling);font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;}}
.lh-momentum-flat{{color:var(--lh-ink-soft);font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;}}
.lh-lead-title{{font-family:'Fraunces',serif;font-weight:600;font-size:2.3rem;line-height:1.04;color:var(--lh-ink);margin:6px 0 14px;letter-spacing:-.02em;}}
.lh-lead-dek{{font-family:'Fraunces',serif;font-size:1.05rem;line-height:1.6;color:var(--lh-ink-soft);max-width:62ch;}}
.lh-pullquote{{border-left:3px solid {beacon};padding:4px 0 4px 18px;font-family:'Fraunces',serif;font-style:italic;font-size:1.1rem;line-height:1.45;color:var(--lh-ink);margin:16px 0 10px;}}
.lh-pullquote cite{{display:block;font-style:normal;font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;color:var(--lh-ink-soft);margin-top:8px;letter-spacing:.06em;}}
.lh-signal{{display:flex;gap:10px;padding:10px 0;border-top:1px solid var(--lh-line);font-size:13px;}}
.lh-signal-plat{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;color:{beacon};width:64px;flex:none;}}
.lh-signal-txt{{line-height:1.4;color:var(--lh-ink);}}
.lh-signal-num{{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--lh-ink-soft);white-space:nowrap;}}
.lh-counter{{background:#062233 !important;color:#d0eaf0 !important;border-radius:8px;padding:20px;}}
.lh-counter-lbl{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:{beacon_2};font-weight:700;margin-bottom:8px;}}
.lh-counter-title{{font-family:'Fraunces',serif;font-size:1.2rem;font-weight:600;line-height:1.2;margin-bottom:10px;color:#d0eaf0;}}
.lh-counter-body{{font-size:13.5px;line-height:1.5;color:rgba(208,234,240,.75);}}
.lh-cc-badge{{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.08em;text-transform:uppercase;border-radius:5px;padding:3px 9px;}}
.lh-cc-draft{{color:rgba(208,234,240,.55);background:rgba(255,255,255,.06);border:1px dashed rgba(208,234,240,.25);}}
.lh-cc-edited{{color:#0fa3b5;background:rgba(10,125,140,.18);border:1px solid rgba(10,125,140,.35);}}
.lh-cc-edit-lbl{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:{beacon};font-weight:700;margin:20px 0 8px;}}
.lh-card{{border-top:2px solid var(--lh-ink);padding-top:14px;}}
.lh-card-top{{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}}
.lh-card-brands{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;color:var(--lh-ink-soft);}}
/* Currents — 3-lens categorization (Competitive / Cultural / Social) */
.lh-cat-head{{display:flex;align-items:center;gap:12px;margin:26px 0 14px;}}
.lh-cat-head .lh-cat-line{{flex:1;height:1px;display:block;background:var(--lh-line-strong);}}
.lh-cat-lbl{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;font-weight:700;white-space:nowrap;}}
.lh-cat-competitive{{color:#c94f35;}}
.lh-cat-cultural{{color:{beacon};}}
.lh-cat-social{{color:#6b4e8c;}}
.lh-card-cat{{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:8.5px;letter-spacing:.1em;text-transform:uppercase;padding:2px 8px;border-radius:4px;font-weight:700;}}
.lh-card-cat-competitive{{background:rgba(201,79,53,.12);color:#c94f35;}}
.lh-card-cat-cultural{{background:rgba(10,125,140,.12);color:{beacon};}}
.lh-card-cat-social{{background:rgba(107,78,140,.12);color:#6b4e8c;}}
.lh-card-title{{font-family:'Fraunces',serif;font-weight:600;font-size:1.2rem;line-height:1.1;color:var(--lh-ink);margin:0 0 8px;}}
.lh-card-body{{font-size:13.5px;line-height:1.5;color:var(--lh-ink-soft);margin:0 0 10px;}}
.lh-spark{{height:28px;display:flex;align-items:flex-end;gap:3px;margin-bottom:10px;}}
.lh-spark i{{flex:1;background:var(--lh-line-strong);border-radius:2px 2px 0 0;display:block;}}
.lh-card-foot{{display:flex;justify-content:space-between;font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;color:var(--lh-ink-soft);padding-top:8px;border-top:1px dotted var(--lh-line);}}
.lh-reach{{font-weight:700;color:var(--lh-ink);}}
.lh-voice{{background:rgba(255,255,255,.75);border:1px solid var(--lh-line);border-left:3px solid var(--lh-line);border-radius:8px;padding:14px 16px;height:100%;}}
.lh-voice-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}}
.lh-voice-plat{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;font-weight:700;}}
.lh-voice-eng{{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:#274d68 !important;}}
.lh-voice-q{{font-family:'Fraunces',serif;font-size:1rem;line-height:1.46;color:#071828 !important;margin-bottom:8px;}}
.lh-voice-bot{{display:flex;justify-content:space-between;border-top:1px dotted var(--lh-line);padding-top:8px;}}
.lh-voice-handle{{font-family:'JetBrains Mono',monospace;font-size:10px;color:#274d68 !important;}}
.lh-voice-rel{{font-family:'JetBrains Mono',monospace;font-size:8.5px;text-transform:uppercase;color:#fff !important;background:#071828;padding:2px 6px;border-radius:3px;}}
.p-reddit-n{{border-left-color:#d93a00;}}.p-reddit-n .lh-voice-plat{{color:#d93a00;}}
.p-tiktok-n{{border-left-color:#111;}}.p-tiktok-n .lh-voice-plat{{color:#111;}}
.p-x-n{{border-left-color:#111;}}.p-x-n .lh-voice-plat{{color:#111;}}
.p-mumsnet-n{{border-left-color:#a4117f;}}.p-mumsnet-n .lh-voice-plat{{color:#a4117f;}}
.p-ig-n{{border-left-color:#c13584;}}.p-ig-n .lh-voice-plat{{color:#c13584;}}
.lh-prov-wrap{{background:#062233 !important;color:#d0eaf0 !important;border-radius:10px;padding:28px 32px;}}
.lh-prov-head-eye{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:{beacon_2};font-weight:700;}}
.lh-prov-head-title{{font-family:'Fraunces',serif;font-weight:600;font-size:1.8rem;margin:8px 0 4px;color:#d0eaf0;}}
.lh-prov-head-sub{{font-family:'Fraunces',serif;font-style:italic;font-size:15px;color:rgba(208,234,240,.55);margin:0 0 20px;}}
.lh-prov{{border-top:1px solid rgba(255,255,255,.15);padding-top:14px;}}
.lh-prov-n{{font-family:'Fraunces',serif;font-size:2.2rem;font-weight:300;color:{beacon_2};display:block;margin-bottom:8px;line-height:1;}}
.lh-prov-text{{font-family:'Fraunces',serif;font-size:1.1rem;line-height:1.42;color:#e8f6fa;margin-bottom:6px;}}
.lh-prov-tag{{font-family:'JetBrains Mono',monospace;font-size:9.5px;text-transform:uppercase;color:rgba(10,125,140,.85);letter-spacing:.06em;}}
.lh-panel{{border:1px solid var(--lh-line);border-radius:8px;background:rgba(255,255,255,.6);margin-bottom:20px;overflow:hidden;}}
.lh-panel-head{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;padding:12px 16px;border-bottom:1px solid var(--lh-line);color:#071828 !important;display:flex;justify-content:space-between;background:rgba(255,255,255,.7);}}
.lh-panel-cnt{{color:{beacon} !important;}}
.lh-brandrow{{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--lh-line);background:rgba(255,255,255,.4);}}
.lh-brandrow:last-child{{border-bottom:none;}}
.lh-av{{width:30px;height:30px;border-radius:6px;flex:none;display:grid;place-items:center;font-family:'Fraunces',serif;font-weight:600;font-size:13px;color:#fff !important;}}
.lh-bn{{font-weight:600;font-size:13px;color:#071828 !important;display:flex;align-items:center;gap:6px;}}
.lh-ours{{font-family:'JetBrains Mono',monospace;font-size:8px;background:{beacon};color:#fff !important;padding:1px 5px;border-radius:3px;}}
.lh-bi{{font-size:10.5px;color:#274d68 !important;}}
.lh-bstat{{margin-left:auto;text-align:right;}}
.lh-bpct{{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12px;color:#071828 !important;}}
.lh-bpct-up{{color:#1a8a6b !important;}}.lh-bpct-down{{color:#c94f35 !important;}}
.lh-bsub{{font-size:9px;font-family:'JetBrains Mono',monospace;text-transform:uppercase;color:#274d68 !important;}}
.lh-alert{{padding:12px 16px;border-bottom:1px solid var(--lh-line);display:flex;gap:10px;background:rgba(255,255,255,.3);}}
.lh-alert:last-child{{border-bottom:none;}}
.lh-sev{{width:4px;border-radius:4px;flex:none;}}
.lh-sev-hi{{background:#c94f35;}}.lh-sev-mid{{background:{beacon};}}.lh-sev-lo{{background:#1a8a6b;}}
.lh-atxt{{font-size:13px;line-height:1.4;color:#071828 !important;}}.lh-atxt b{{font-weight:600;color:#071828 !important;}}
.lh-atime{{font-family:'JetBrains Mono',monospace;font-size:9.5px;text-transform:uppercase;color:#274d68 !important;margin-top:4px;}}
.lh-digest{{padding:16px;background:rgba(255,255,255,.3);}}
.lh-digest-p{{font-family:'Fraunces',serif;font-size:14px;line-height:1.6;color:#071828 !important;margin:0 0 12px;}}
.lh-next{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;color:#274d68 !important;text-align:center;padding:10px;border-top:1px dashed var(--lh-line);}}
/* Save button (💾) — transparent bg, turns beacon on hover */
div[data-testid="stButton"] > button {{
  background: transparent !important;
  border: 1px solid rgba(157,196,216,.4) !important;
  border-radius: 6px !important;
  color: #274d68 !important;
  font-size: 15px !important;
  line-height: 1 !important;
  padding: 3px 8px !important;
  min-height: 28px !important;
  height: 28px !important;
  box-shadow: none !important;
  transition: all .15s !important;
}}
div[data-testid="stButton"] > button:hover {{
  border-color: {beacon} !important;
  color: {beacon} !important;
  background: transparent !important;
}}
</style>"""


# ── Masthead iframe (static — no interaction needed) ──────────────────────────

def build_masthead_html(content: dict, signals: list, client: str, tagline: str) -> str:
    """Top section: agency bar + logo + sweep + strongest current strip."""
    sw        = content.get("sweep", {})
    lead      = content.get("lead", {})
    today_str = datetime.utcnow().strftime("%A, %d %B %Y")
    vol_no    = f"Vol. I · No. {datetime.utcnow().strftime('%j')}"
    sig_n     = len(signals)
    sig_display = f"{sig_n/1000:.1f}K" if sig_n < 1_000_000 else f"{sig_n/1_000_000:.2f}M"
    src_pills   = sources_pills(signals)
    beacon      = CLIENT_BEACON_COLOR
    beacon_2    = CLIENT_BEACON_2
    pill_color  = CLIENT_PILL_COLOR
    agency      = e(AGENCY_NAME)
    # Strongest current: prefer the strategic directive; fall back to first current headline
    _currents = content.get("currents", [])
    _strongest = (
        lead.get("countercurrent_title", "").strip() or
        (_currents[0].get("headline", "") if _currents else "") or
        "Sweeping currents — check back after the next ingestion run."
    )
    _strongest_esc = e(_strongest)

    return f"""<!DOCTYPE html><html lang="en-GB"><head>
<meta charset="UTF-8"/>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,400..700&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet"/>
<style>
:root{{--paper:#ebf2f7;--paper-2:#dce9f2;--ink:#071828;--ink-soft:#274d68;--line:#9dc4d8;--line-strong:#6ea8c4;--beacon:{beacon};--beacon-2:{beacon_2};--deep:#062233;--atlantic:#0a4a6e;--rising:#1a8a6b;--falling:#c94f35;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:var(--paper);color:var(--ink);font-family:'Inter',sans-serif;-webkit-font-smoothing:antialiased;background-image:radial-gradient(ellipse 80% 40% at 50% -10%,rgba(10,125,140,.08),transparent),radial-gradient(ellipse 60% 30% at 90% 110%,rgba(6,34,51,.05),transparent);}}
.wrap{{max-width:1240px;margin:0 auto;padding:0 28px;}}
.agency-bar{{background:var(--ink);color:rgba(255,255,255,.45);font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;display:flex;justify-content:space-between;align-items:center;padding:7px 28px;}}
.agency-bar .am{{color:#fff;font-weight:700;letter-spacing:.22em;}}
.masthead{{border-bottom:3px double var(--ink);padding-top:22px;}}
.masthead-top{{display:flex;justify-content:space-between;align-items:flex-end;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-soft);padding-bottom:14px;border-bottom:1px solid var(--line);}}
.edition{{display:flex;gap:22px;align-items:center;}}
.live{{display:inline-flex;align-items:center;gap:7px;color:var(--ink);font-weight:700;}}
.dot{{width:8px;height:8px;border-radius:50%;background:var(--beacon);animation:pulse 2.4s infinite;}}
@keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(10,125,140,.5);}}70%{{box-shadow:0 0 0 10px rgba(10,125,140,0);}}100%{{box-shadow:0 0 0 0 rgba(10,125,140,0);}}}}
.clientbar{{display:flex;justify-content:center;align-items:center;gap:10px;padding:12px 0 2px;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-soft);}}
.pill{{background:{pill_color};color:#fff;padding:3px 11px;border-radius:3px;font-weight:700;letter-spacing:.08em;}}
.title-row{{display:flex;align-items:center;justify-content:center;gap:26px;padding:8px 0 10px;}}
.beacon-mark{{position:relative;width:54px;height:54px;flex:none;}}
.tower{{position:absolute;left:50%;bottom:0;transform:translateX(-50%);width:14px;height:34px;background:linear-gradient(var(--ink),#1a3d52);clip-path:polygon(28% 0,72% 0,100% 100%,0 100%);}}
.lamp{{position:absolute;left:50%;top:7px;transform:translateX(-50%);width:14px;height:11px;background:var(--beacon);border-radius:3px 3px 0 0;box-shadow:0 0 16px 4px rgba(10,125,140,.55);z-index:2;}}
.beam{{position:absolute;left:50%;top:12px;width:0;height:0;transform-origin:left center;border-top:16px solid transparent;border-bottom:16px solid transparent;border-left:64px solid rgba(15,163,181,.28);animation:sweep 7s ease-in-out infinite;}}
@keyframes sweep{{0%,100%{{transform:rotate(-32deg);opacity:.2;}}50%{{transform:rotate(28deg);opacity:.5;}}}}
h1.logo{{font-family:'Fraunces',serif;font-weight:500;font-size:58px;letter-spacing:.01em;margin:0;line-height:.95;text-align:center;}}
h1.logo .the{{display:block;font-size:13px;letter-spacing:.5em;font-weight:400;margin-bottom:6px;color:var(--ink-soft);font-family:'JetBrains Mono',monospace;text-transform:uppercase;}}
.tagline{{text-align:center;font-family:'Fraunces',serif;font-style:italic;font-size:15px;color:var(--ink-soft);padding:6px 0 16px;}}
.sweep{{display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid var(--line);background:rgba(255,255,255,.4);}}
.cell{{padding:16px 18px;border-right:1px solid var(--line);}}
.cell:last-child{{border-right:none;}}
.k{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--ink-soft);}}
.v{{font-family:'Fraunces',serif;font-size:30px;font-weight:500;margin-top:4px;line-height:1;}}
.sources-line{{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px;}}
.src{{font-family:'JetBrains Mono',monospace;font-size:9.5px;padding:2px 6px;border:1px solid var(--line-strong);border-radius:20px;color:var(--ink-soft);background:var(--paper-2);}}
.src.on{{color:var(--beacon);border-color:var(--beacon);}}
.src .d{{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--beacon);margin-right:4px;vertical-align:middle;}}
.current-strip{{display:flex;align-items:baseline;gap:20px;padding:15px 0 20px;border-top:1px solid var(--line);margin-top:0;}}
.current-label{{font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.18em;text-transform:uppercase;color:var(--beacon);font-weight:700;white-space:nowrap;flex:none;padding-top:2px;}}
.current-text{{font-family:'Fraunces',serif;font-style:italic;font-size:17px;color:var(--ink);line-height:1.4;flex:1;}}
.current-arrow{{color:var(--beacon);font-style:normal;margin-right:6px;}}
</style></head>
<body>
<div class="agency-bar">
  <span>Cultural Intelligence Platform · Powered by Countercurrent</span>
  <span class="am">{agency}</span>
</div>
<div class="wrap">
  <header class="masthead">
    <div class="masthead-top">
      <div class="edition"><span>{vol_no}</span><span>{today_str}</span></div>
      <div class="edition">
        <span class="live"><span class="dot"></span> Sweeping live</span>
        <span>Leadership Edition</span>
      </div>
    </div>
    <div class="clientbar"><span class="pill">Client</span> {e(client)}</div>
    <div class="title-row">
      <div class="beacon-mark"><span class="beam"></span><span class="lamp"></span><span class="tower"></span></div>
      <h1 class="logo"><span class="the">The</span>Lighthouse</h1>
    </div>
    <p class="tagline">{e(tagline)}</p>
  </header>
  <section class="sweep">
    <div class="cell"><div class="k">Signals scanned · 24h</div><div class="v" id="sc">{sig_display}</div></div>
    <div class="cell"><div class="k">Currents surfaced</div><div class="v">{sw.get("currents_surfaced","—")}</div></div>
    <div class="cell"><div class="k">Rising fast</div><div class="v" style="color:var(--rising)">{sw.get("rising_fast","—")}</div></div>
    <div class="cell"><div class="k">Needs a human</div><div class="v" style="color:var(--beacon)">{sw.get("needs_human","—")}</div></div>
    <div class="cell"><div class="k">Sources active</div><div class="sources-line">{src_pills}</div></div>
  </section>
  <div class="current-strip">
    <div class="current-label">▲ Today's strongest current</div>
    <div class="current-text"><span class="current-arrow">&#x201C;</span>{_strongest_esc}&#x201D;</div>
  </div>
</div>
<script>
var el=document.getElementById('sc');var n={sig_n};
if(el&&n>0){{setInterval(function(){{n+=Math.floor(Math.random()*4+1);el.textContent=n>=1000000?(n/1000000).toFixed(2)+'M':(n/1000).toFixed(1)+'K';}},1800);}}
</script>
</body></html>"""


# ── Raw Signal Feed ───────────────────────────────────────────────────────────

SOURCE_COLORS = {
    "reddit": "#d44800",
    "tiktok": "#0fa3b5",
    "rss":    "#6ea8c4",
    "web":    "#1a8a6b",
}
SOURCE_LABELS = {
    "reddit": "Reddit",
    "tiktok": "TikTok",
    "rss":    "RSS",
    "web":    "Web",
}

def _render_raw_signals(signals: list, topic_tags: list) -> None:
    """Show real captured signals with direct links, filterable by platform."""
    if not signals:
        return

    beacon   = CLIENT_BEACON_COLOR
    beacon_2 = CLIENT_BEACON_2

    # Score signals by topic relevance
    topic_words = set(" ".join(topic_tags).lower().split())
    def relevance(s):
        text = f"{s.get('title','')} {s.get('content','')}".lower()
        return sum(1 for w in topic_words if w in text)

    scored = sorted(
        [s for s in signals if s.get("url","").startswith("http")],
        key=lambda s: (-relevance(s), s.get("timestamp",""))
    )

    # Available platforms in this dataset
    platforms = sorted({s.get("source","other").lower() for s in scored})

    st.markdown(f"""
<div style="border-top:2px solid #071828;padding-top:18px;margin:32px 0 16px;">
  <span style="font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.16em;
        text-transform:uppercase;color:{beacon};font-weight:700;">◉ Raw Signal Feed</span>
  <div style="font-family:'Fraunces',serif;font-weight:600;font-size:2rem;
        margin:10px 0 6px;color:#071828;">Original posts from the sweep</div>
  <div style="font-family:'Fraunces',serif;font-style:italic;font-size:15px;
        color:#274d68;max-width:72ch;">Real content captured directly from the platforms — unfiltered, unedited.
        Click <b>↗ view post</b> to open the original publication.</div>
</div>""", unsafe_allow_html=True)

    # Platform filter
    filter_cols = st.columns(len(platforms) + 1)
    selected_src = st.session_state.get("raw_signal_filter", "all")

    with filter_cols[0]:
        if st.button("All", key="rsf_all",
                     type="primary" if selected_src == "all" else "secondary"):
            st.session_state["raw_signal_filter"] = "all"
            st.rerun()

    for ci, plat in enumerate(platforms):
        with filter_cols[ci + 1]:
            col = SOURCE_COLORS.get(plat, "#9dc4d8")
            label = SOURCE_LABELS.get(plat, plat.title())
            if st.button(label, key=f"rsf_{plat}",
                         type="primary" if selected_src == plat else "secondary"):
                st.session_state["raw_signal_filter"] = plat
                st.rerun()

    # Filter + cap at 30
    selected_src = st.session_state.get("raw_signal_filter", "all")
    filtered = [
        s for s in scored
        if selected_src == "all" or s.get("source","").lower() == selected_src
    ][:30]

    if not filtered:
        st.caption("No signals found for this filter.")
        return

    sig_cols = st.columns(3, gap="medium")
    for i, s in enumerate(filtered):
        src    = s.get("source","web").lower()
        color  = SOURCE_COLORS.get(src, "#9dc4d8")
        label  = SOURCE_LABELS.get(src, src.title())
        title  = s.get("title","") or "—"
        body   = s.get("content","")[:200]
        if len(s.get("content","")) > 200:
            body += "…"
        ts     = s.get("timestamp","")[:10]
        url    = s.get("url","")

        with sig_cols[i % 3]:
            st.markdown(f"""
<div style="background:rgba(255,255,255,.7);border:1px solid #9dc4d8;
     border-left:3px solid {color};border-radius:8px;padding:14px 16px;
     margin-bottom:14px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <span style="font-family:'JetBrains Mono',monospace;font-size:9px;
          text-transform:uppercase;letter-spacing:.1em;color:{color};font-weight:700;">
      ● {label}</span>
    <span style="font-family:'JetBrains Mono',monospace;font-size:9px;color:#9dc4d8;">{ts}</span>
  </div>
  <div style="font-family:'Fraunces',serif;font-size:14px;font-weight:600;
       color:#071828;line-height:1.35;margin-bottom:8px;">{e(title[:90])}</div>
  <div style="font-size:12.5px;color:#274d68;line-height:1.55;margin-bottom:12px;">{e(body)}</div>
  <a href="{e(url)}" target="_blank" rel="noopener"
     style="font-family:'JetBrains Mono',monospace;font-size:9.5px;
            letter-spacing:.06em;text-transform:uppercase;color:{color};
            text-decoration:none;border-bottom:1px solid {color};
            padding-bottom:1px;">↗ view post</a>
</div>""", unsafe_allow_html=True)


# ── Native Streamlit section renderers ────────────────────────────────────────

def _mdir(d):
    return {"up": "▲", "down": "▼"}.get(d, "●")
def _mcls(d):
    return {"up": "lh-momentum-up", "down": "lh-momentum-down"}.get(d, "lh-momentum-flat")

# ── "More Currents" — 3-lens categorization ───────────────────────────────────
# The dispatch groups currents into three lenses (per the Lighthouse.ai wireframe):
#   competitive — what rival brands are doing
#   cultural    — broader shifts in culture/conversation
#   social      — chatter, memes, platform-native noise
CATEGORY_META = {
    "competitive": ("⚔", "Competitive Maneuvers", "lh-cat-competitive"),
    "cultural":    ("〰", "Cultural Waves",        "lh-cat-cultural"),
    "social":      ("💬", "Social Chatter",        "lh-cat-social"),
}
CATEGORY_ORDER = ["competitive", "cultural", "social"]

_CAT_KEYWORDS = {
    "competitive": [
        "competitor", "rival", "brand war", "market share", "launch", "campaign",
        " vs ", "competing", "rebrand", "ad spend", "pricing", "shelf",
    ],
    "social": [
        "tiktok", "reddit", "meme", "viral", "comment section", "thread",
        "forum", "hashtag", "duet", "stitch", "influencer", "creator",
    ],
}

def _card_category(card: dict) -> str:
    """Return 'competitive' | 'cultural' | 'social' for a card.

    Uses the explicit `category` field when present (new dispatches). Older
    saved dispatches won't have it, so fall back to a keyword heuristic over
    the card's tags/title/body — defaulting to 'cultural' (the broadest lens).
    """
    cat = (card.get("category") or "").strip().lower()
    if cat in CATEGORY_META:
        return cat
    text = f"{card.get('tags','')} {card.get('title','')} {card.get('body','')}".lower()
    for cat_name in ("competitive", "social"):
        if any(kw in text for kw in _CAT_KEYWORDS[cat_name]):
            return cat_name
    return "cultural"

def _save_button(label: str, type_: str, title: str, content_str: str, key: str, user: str):
    """Renders the dispatch card's "+ Add to project" control — internal only.

    Per the wireframe's CAPTURE → COLLECT step, this combines "save to board"
    and "assign to project folder" into a single popover. Clients never see
    this — they get the clean feed (the dispatch view has no add-to-project
    affordance for them).
    """
    if IS_CLIENT:
        return

    items = load_curadoria()
    existing = next((i for i in items if i["user"] == user and i["title"] == title), None)
    folders = load_project_folders()
    folder_map = {f["id"]: f["name"] for f in folders}
    current_fids = [fid for fid in (existing.get("folder_ids") or []) if fid in folder_map] if existing else []

    trigger_icon = "✓" if existing else "🗂️"
    trigger_help = "In project — manage folders" if existing else "+ Add to project"

    with st.popover(trigger_icon, help=trigger_help):
        st.markdown("**+ Add to project**" if not existing else "**✓ Saved to your board**")
        if folders:
            selected = st.multiselect(
                "Project folders", options=[f["id"] for f in folders],
                default=current_fids, format_func=lambda fid: folder_map.get(fid, fid),
                key=f"addproj_{key}", label_visibility="collapsed",
                placeholder="Project folder(s) — optional",
            )
        else:
            st.caption("No project folders yet — create one in the **Projects** tab.")
            selected = []

        if st.button("Add to project" if not existing else "Update", key=f"addprojbtn_{key}", use_container_width=True):
            if existing:
                set_item_folders(existing["id"], selected)
                st.toast(f"✓ Updated, {user}!")
            else:
                add_curadoria_item(user, type_, title, content_str)
                for it in load_curadoria():
                    if it["user"] == user and it["title"] == title:
                        set_item_folders(it["id"], selected)
                        break
                st.toast(f"✓ Added to project, {user}!")
            st.rerun()

        if existing:
            if st.button("Remove from board", key=f"addprojrm_{key}", use_container_width=True):
                remove_curadoria_item(existing["id"])
                st.toast("Removed from your board.")
                st.rerun()


_VOICE_PLATFORM_CSS_MAP = {
    "p-reddit": "p-reddit-n", "p-tiktok": "p-tiktok-n",
    "p-x": "p-x-n", "p-mumsnet": "p-mumsnet-n", "p-ig": "p-ig-n",
}


def _render_voices_header() -> None:
    """Section header/intro for "What people are actually saying".

    Split out from _render_voices_and_provocations so it (and the first
    voice card) can render always-visible above the "▼ More to explore"
    expander — see render_content_sections.
    """
    st.markdown('<div id="lh-sec-voices"></div>', unsafe_allow_html=True)
    st.markdown("""
<div style="border-top:2px solid #071828;padding-top:18px;margin:8px 0 20px">
  <span class="lh-eyebrow">◎ Editorial Synthesis · Claude-Composed Voices</span>
  <div style="font-family:'Fraunces',serif;font-weight:600;font-size:2rem;margin:10px 0 6px;color:#071828">What people are actually saying</div>
  <div style="font-family:'Fraunces',serif;font-style:italic;font-size:15px;color:#274d68;max-width:72ch;margin-bottom:10px">Raw signal texture from this sweep — the language and feelings real people attach to the category. Steal the language.</div>
  <div style="font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.06em;color:#9dc4d8;border-left:2px solid #9dc4d8;padding-left:10px;">These voices are editorial composites written by Claude from real signals — condensed for clarity. See the <b>Raw Signal Feed</b> below for the original posts with direct links.</div>
</div>""", unsafe_allow_html=True)


def _render_voice_card(v: dict, idx: int, user: str) -> None:
    """Renders one Voice quote card + its 🔖 save button."""
    pcls = _VOICE_PLATFORM_CSS_MAP.get(v.get("platform_class", ""), "")
    col_v, col_vs = st.columns([8, 1])
    with col_v:
        _v_url  = v.get("url", "")
        _v_link = (
            '<a href="' + e(_v_url) + '" target="_blank" rel="noopener" '
            'style="margin-left:auto;font-family:JetBrains Mono,monospace;'
            'font-size:9px;letter-spacing:.06em;text-transform:uppercase;'
            'color:#0a7d8c;text-decoration:none;">↗ source</a>'
        ) if _v_url else ""
        st.markdown(f"""
<div class="lh-voice {pcls}">
  <div class="lh-voice-top">
    <span class="lh-voice-plat">● {e(v.get("platform_label",""))}</span>
    <span class="lh-voice-eng">{e(v.get("engagement",""))}</span>
  </div>
  <div class="lh-voice-q">&ldquo;{e(v.get("quote",""))}&rdquo;</div>
  <div class="lh-voice-bot">
    <span class="lh-voice-handle">{e(v.get("handle",""))}</span>
    <span class="lh-voice-rel">{e(v.get("rel_tag",""))}</span>
    {_v_link}
  </div>
</div>""", unsafe_allow_html=True)
    with col_vs:
        _save_button("🔖",
            f"Voice · {v.get('platform_label','')}",
            v.get("quote","")[:80],
            v.get("quote",""),
            f"save_voice_{idx}", user)


def _render_voices_and_provocations(lead: dict, voices: list, provs: list, user: str, start_idx: int = 0) -> None:
    """Remaining Voices grid, Raw Signal Feed, and Provocations.

    Pulled out of render_content_sections() so it can be tucked behind a
    "▼ More to explore" expander on the Dispatches tab — Lead Current and
    the Countercurrent stay front-and-center, this is reading material for
    when the team wants to go deeper. `start_idx` skips the voice(s) already
    shown always-visible above the expander (see _render_voices_header /
    _render_voice_card).
    """
    # ── VOICES (grid) ────────────────────────────────────────────────────────
    remaining_voices = voices[start_idx:9]
    if remaining_voices:
        voice_cols = st.columns(3, gap="medium")
        for i, v in enumerate(remaining_voices):
            with voice_cols[i % 3]:
                _render_voice_card(v, start_idx + i, user)

    # ── RAW SIGNAL FEED ───────────────────────────────────────────────────────
    _render_raw_signals(load_signals(), lead.get("topic_tags", []))

    # ── PROVOCATIONS — single HTML block, no Streamlit columns (avoids gap bleed) ──
    st.markdown('<div id="lh-sec-provs"></div>', unsafe_allow_html=True)
    prov_items_html = ""
    for p in provs[:3]:
        prov_items_html += f"""
  <div style="border-top:1px solid rgba(255,255,255,.15);padding-top:18px;">
    <span style="font-family:'Fraunces',serif;font-size:2.2rem;font-weight:300;color:{CLIENT_BEACON_2};display:block;margin-bottom:10px;line-height:1;">{e(p.get("n",""))}</span>
    <div style="font-family:'Fraunces',serif;font-size:1.05rem;line-height:1.44;color:#e8f6fa;margin-bottom:10px;">{e(p.get("text",""))}</div>
    <span style="font-family:'JetBrains Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;color:rgba(10,125,140,.85);">{e(p.get("tag",""))}</span>
  </div>"""

    st.markdown(f"""
<div style="background:#062233;color:#d0eaf0;border-radius:10px;padding:28px 32px 24px;margin:0 0 4px;">
  <div style="font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:{CLIENT_BEACON_2};font-weight:700;margin-bottom:6px;">◐ To Close · The Countercurrent</div>
  <div style="font-family:'Fraunces',serif;font-weight:600;font-size:1.8rem;margin:4px 0 6px;color:#d0eaf0;">Three provocations for the room</div>
  <div style="font-family:'Fraunces',serif;font-style:italic;font-size:15px;color:rgba(208,234,240,.55);margin:0 0 22px;">Deliberately unfinished questions drawn from today's currents — not answers, but opening lines to push the team past the obvious.</div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:28px;">
    {prov_items_html}
  </div>
</div>""", unsafe_allow_html=True)

    # Save buttons sit just below the dark block, one per column
    prov_save_cols = st.columns(3, gap="large")
    for i, p in enumerate(provs[:3]):
        with prov_save_cols[i]:
            _save_button("🔖",
                f"Provocation {p.get('n','')}",
                p.get("text",""),
                p.get("tag",""),
                f"save_prov_{i}", user)


def _render_category_group(cat: str, group: list, user: str) -> None:
    """Renders one Cultural/Social/Competitive lens: header + its card(s).

    Pulled out of render_content_sections() so the first non-empty lens can
    render always-visible (above the "▼ More currents" expander) while any
    remaining lenses stay tucked behind it.
    """
    icon, label, css_cls = CATEGORY_META[cat]
    st.markdown(f"""
<div class="lh-cat-head">
  <span class="lh-cat-lbl {css_cls}">{icon} {e(label)}</span>
  <span class="lh-cat-line"></span>
</div>""", unsafe_allow_html=True)

    # Only split into a 2-column grid when there's enough cards to fill it —
    # a lone card in a 2-col grid left a big empty gap and made the save icon
    # look detached. A single card gets the full width instead.
    n_cards = len(group)
    if n_cards >= 2:
        card_cols = st.columns(2, gap="large")
    else:
        card_cols = [st.container()]

    for j, (i, card) in enumerate(group):
        d = card.get("momentum_dir", "up")
        spark_bars = "".join(f'<i style="height:{v}%"></i>' for v in (card.get("spark") or [30,45,55,65,75,82,90]))
        with card_cols[j % len(card_cols)]:
            col_card, col_card_save = st.columns([10, 1])
            with col_card:
                st.markdown(f"""
<div class="lh-card">
  <div class="lh-card-top">
    <span class="{_mcls(d)}">{_mdir(d)} {e(card.get("momentum_pct",""))}</span>
    <span class="lh-card-brands">{e(card.get("tags",""))}</span>
    <span class="lh-card-cat {css_cls.replace('lh-cat-', 'lh-card-cat-')}">{icon} {e(label)}</span>
  </div>
  <div class="lh-card-title">{e(card.get("title",""))}</div>
  <div class="lh-card-body">{e(card.get("body",""))}</div>
  <div class="lh-spark">{spark_bars}</div>
  <div class="lh-card-foot"><span>{e(card.get("sources",""))}</span><span class="lh-reach">{e(card.get("reach",""))}</span></div>
</div>""", unsafe_allow_html=True)
            with col_card_save:
                st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
                _save_button("🔖", f"Card — {card.get('tags','')}",
                    card.get("title",""), card.get("body",""),
                    f"save_card_{i}", user)


def render_content_sections(content: dict, user: str, show_competitive: bool = True):
    """Renders lead, cards, rail, voices, provocations with native Streamlit + save buttons."""
    beacon   = CLIENT_BEACON_COLOR
    beacon_2 = CLIENT_BEACON_2

    # Inject CSS once
    st.markdown(_native_css(beacon, beacon_2), unsafe_allow_html=True)

    lead   = content.get("lead", {})
    cards  = content.get("cards", [])
    voices = content.get("voices", [])
    provs  = content.get("provocations", [])
    alerts = content.get("alerts", [])
    sw     = content.get("sweep", {})

    dispatch_id = content.get("_dispatch_id", "fallback")

    ld = lead.get("momentum_dir", "up")

    # ── LEAD + RAIL grid ──────────────────────────────────────────────────────
    col_main, col_rail = st.columns([13, 5], gap="large")

    with col_main:
        # Lead header
        st.markdown(f"""
<div class="lh-section-rule">
  <div class="lh-meta">
    <span class="lh-tag">Lead Current</span>
    <span>{" · ".join(e(t) for t in lead.get("topic_tags",[]))}</span>
    <span>Relevance: <b>{e(lead.get("relevance","—"))}</b></span>
    <span class="{_mcls(ld)}">{_mdir(ld)} {e(lead.get("momentum_pct",""))}/{e(lead.get("momentum_period",""))}</span>
  </div>
</div>""", unsafe_allow_html=True)

        # Lead title + dek + save button
        col_lead_text, col_lead_save = st.columns([20, 1])
        with col_lead_text:
            st.markdown(f"""
<div class="lh-lead-title">{e(lead.get("title",""))}</div>
<div class="lh-lead-dek">{e(lead.get("dek",""))}</div>""", unsafe_allow_html=True)
        with col_lead_save:
            _save_button("🔖", "Lead Current",
                lead.get("title",""),
                lead.get("countercurrent_title","") + " — " + lead.get("countercurrent_body",""),
                "save_lead_main", user)

        # Pullquote + signal stack
        st.markdown(f"""
<div class="lh-pullquote">
  &ldquo;{e(lead.get("pullquote",""))}&rdquo;
  <cite>— {e(lead.get("pullquote_cite",""))}</cite>
</div>
<div>
{"".join(f'<div class="lh-signal"><span class="lh-signal-plat">{e(s.get("platform",""))}</span><span class="lh-signal-txt">{e(s.get("text",""))}</span><span class="lh-signal-num">{e(s.get("num",""))}</span></div>' for s in lead.get("signal_stack",[]))}
</div>""", unsafe_allow_html=True)

        # ── Countercurrent box — hidden per Jul 2026 meeting feedback ──────────
        # Patrick: "we just want it pulling signals and giving a brief synopsis —
        # we don't want it solving the thing for us."
        # Re-enable when the team is ready to own the editorial synthesis step.
        #
        # cc_overrides = load_countercurrent_overrides()
        # cc_override  = cc_overrides.get(dispatch_id)
        # cc_ai_title  = lead.get("countercurrent_title", "")
        # cc_ai_body   = lead.get("countercurrent_body", "")
        # cc_title     = cc_override["title"] if cc_override else cc_ai_title
        # cc_body      = cc_override["body"]  if cc_override else cc_ai_body
        # cc_edit_key  = f"cc_editing_{dispatch_id}"
        # ... (full block preserved below for when we re-enable)
        #
        # ─────────────────────────────────────────────────────────────────────

        # Section divider
        st.markdown('<div id="lh-sec-currents"></div>', unsafe_allow_html=True)
        st.markdown(f"""
<div style="display:flex;align-items:center;gap:14px;margin:28px 0 6px">
  <span class="lh-eyebrow">More currents worth watching</span>
  <span style="flex:1;height:1px;background:#6ea8c4;display:block"></span>
</div>""", unsafe_allow_html=True)

        # Group cards into the 3 Lighthouse lenses: Competitive / Cultural / Social
        cards_by_cat = {c: [] for c in CATEGORY_ORDER}
        for i, card in enumerate(cards):
            cards_by_cat[_card_category(card)].append((i, card))

        nonempty_groups = []
        for cat in CATEGORY_ORDER:
            if cat == "competitive" and not show_competitive:
                continue
            group = cards_by_cat[cat]
            if group:
                nonempty_groups.append((cat, group))

        # The first lens stays always-visible — otherwise, with both "more"
        # sections collapsed, the left column ends well short of the taller
        # rail sidebar and leaves a big empty gap. Any remaining lenses stay
        # tucked behind the ▼ expander, collapsed by default.
        if nonempty_groups:
            _render_category_group(nonempty_groups[0][0], nonempty_groups[0][1], user)

        if len(nonempty_groups) > 1:
            with st.expander("▼  More currents — Cultural & Social lenses", expanded=False):
                for cat, group in nonempty_groups[1:]:
                    _render_category_group(cat, group, user)

        # ── VOICES / RAW SIGNAL FEED / PROVOCATIONS ─────────────────────────────
        # Kept inside col_main, right under "More currents", so the two
        # collapsible sections sit together instead of being separated by the
        # taller rail sidebar (Share of Voice / Alerts / Briefing) on the right.
        # Same always-visible-first-item pattern as above: the header + first
        # Voice card stay visible, the rest sit behind the ▼ expander.
        _render_voices_header()
        if voices:
            _render_voice_card(voices[0], 0, user)

        with st.expander("▼  More to explore — Voices, Raw Signal Feed & Provocations", expanded=False):
            _render_voices_and_provocations(lead, voices, provs, user, start_idx=1)

    # ── RAIL SIDEBAR ──────────────────────────────────────────────────────────
    with col_rail:
        # Share of Voice (static — no save button needed)
        st.markdown("""
<div class="lh-panel">
  <div class="lh-panel-head">Share Of Voice · Soup <span class="lh-panel-cnt">7d</span></div>
  <div class="lh-brandrow"><div class="lh-av" style="background:#0a7d8c">H</div><div><div class="lh-bn">Heinz Cream of Tomato <span class="lh-ours">OURS</span></div><div class="lh-bi">Can · flagship</div></div><div class="lh-bstat"><div class="lh-bpct lh-bpct-up">▲ 41%</div><div class="lh-bsub">Conversation</div></div></div>
  <div class="lh-brandrow"><div class="lh-av" style="background:#0a4a6e">H</div><div><div class="lh-bn">Heinz Soup of the Day <span class="lh-ours">OURS</span></div><div class="lh-bi">Pouch · convenience</div></div><div class="lh-bstat"><div class="lh-bpct lh-bpct-up">▲ 63%</div><div class="lh-bsub">Conversation</div></div></div>
  <div class="lh-brandrow"><div class="lh-av" style="background:#3a6e3a">C</div><div><div class="lh-bn">Cully &amp; Sully</div><div class="lh-bi">Pot · competitor</div></div><div class="lh-bstat"><div class="lh-bpct lh-bpct-up">▲ 28%</div><div class="lh-bsub">Gaining</div></div></div>
  <div class="lh-brandrow"><div class="lh-av" style="background:#6b4e8c">G</div><div><div class="lh-bn">New Covent Garden</div><div class="lh-bi">Carton · competitor</div></div><div class="lh-bstat"><div class="lh-bpct" style="color:#6ea8c4">● 2%</div><div class="lh-bsub">Flat</div></div></div>
  <div class="lh-brandrow"><div class="lh-av" style="background:#8a6a3a">B</div><div><div class="lh-bn">Batchelors Cup-a-Soup</div><div class="lh-bi">Sachet · declining</div></div><div class="lh-bstat"><div class="lh-bpct lh-bpct-down">▼ 19%</div><div class="lh-bsub">Fading</div></div></div>
</div>""", unsafe_allow_html=True)

        # Alerts
        alerts_html_native = "".join(
            f'<div class="lh-alert"><div class="lh-sev lh-sev-{e(a.get("sev","mid"))}"></div>'
            f'<div><div class="lh-atxt">{a.get("text","")}</div>'
            f'<div class="lh-atime">{e(a.get("time",""))}</div></div></div>'
            for a in alerts[:3]
        )
        st.markdown(f"""
<div class="lh-panel">
  <div class="lh-panel-head">Needs A Human <span class="lh-panel-cnt">{len(alerts)} open</span></div>
  {alerts_html_native}
</div>""", unsafe_allow_html=True)

        # Briefing
        st.markdown(f"""
<div class="lh-panel">
  <div class="lh-panel-head">The 07:00 Briefing</div>
  <div class="lh-digest">
    <div class="lh-digest-p">&ldquo;{e(content.get("briefing",""))}&rdquo;</div>
  </div>
  <div class="lh-next">◷ Next sweep on demand</div>
</div>""", unsafe_allow_html=True)


# ── Topic / Signal Map (D3 force-directed) ────────────────────────────────────

def render_topic_map(content: dict) -> None:
    """Render a D3 force-directed network of topics extracted from the dispatch."""
    import json as _json

    beacon   = CLIENT_BEACON_COLOR
    beacon_2 = CLIENT_BEACON_2

    # ── Extract topics & build graph ──────────────────────────────────────────
    topic_weight: dict = {}
    cooc: dict = {}

    def add_t(t: str, w: float):
        t = t.strip().lower()
        if t and len(t) > 2:
            topic_weight[t] = topic_weight.get(t, 0) + w

    def add_e(a: str, b: str, w: float):
        a, b = a.strip().lower(), b.strip().lower()
        if a and b and a != b:
            key = tuple(sorted([a, b]))
            cooc[key] = cooc.get(key, 0) + w

    lead  = content.get("lead", {})
    ltags = lead.get("topic_tags", [])
    for t in ltags:
        add_t(t, 5)
    for i, t1 in enumerate(ltags):
        for t2 in ltags[i + 1:]:
            add_e(t1, t2, 3)

    for card in content.get("cards", []):
        raw   = card.get("tags", "").replace("·", ",")
        ctags = [t.strip() for t in raw.split(",") if t.strip()]
        for t in ctags:
            add_t(t, 3)
        for i, t1 in enumerate(ctags):
            for t2 in ctags[i + 1:]:
                add_e(t1, t2, 2)

    for v in content.get("voices", []):
        rt = v.get("rel_tag", "").strip()
        if rt:
            add_t(rt, 1)
            for lt in ltags[:2]:
                add_e(rt, lt, 0.8)

    if not topic_weight:
        return

    nodes = [{"id": t, "w": round(w, 1)} for t, w in topic_weight.items()]
    links = [
        {"source": k[0], "target": k[1], "w": round(v, 1)}
        for k, v in cooc.items()
        if k[0] in topic_weight and k[1] in topic_weight
    ]

    nodes_json = _json.dumps(nodes)
    links_json = _json.dumps(links)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Fraunces:ital,opsz,wght@0,9..144,400;1,9..144,400&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{background:#062233;overflow:hidden;width:100%;height:100%;}}
#header{{padding:20px 28px 0;display:flex;align-items:baseline;gap:16px;}}
.eye{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.18em;
      text-transform:uppercase;color:{beacon_2};font-weight:700;}}
.ttl{{font-family:'Fraunces',serif;font-size:17px;font-weight:500;color:#d0eaf0;}}
.sub{{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:rgba(208,234,240,.45);
      letter-spacing:.06em;text-transform:uppercase;margin-left:auto;}}
#chart{{width:100%;height:380px;display:block;}}
.node-label{{
  font-family:'JetBrains Mono',monospace;
  fill:#c8e8f0;
  pointer-events:none;
  text-shadow:0 1px 5px rgba(6,34,51,.95),0 0 10px rgba(6,34,51,.7);
  dominant-baseline:middle;
}}
</style>
</head>
<body>
<div id="header">
  <span class="eye">◎ Signal Map</span>
  <span class="ttl">Topic landscape · this edition</span>
  <span class="sub">Drag nodes · hover to highlight</span>
</div>
<svg id="chart"></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const nodes = {nodes_json};
const links = {links_json};

const W = document.getElementById('chart').clientWidth || 960;
const H = 380;
const svg = d3.select('#chart').attr('width',W).attr('height',H);

// Soft glow backdrop
const defs = svg.append('defs');
const glow = defs.append('filter').attr('id','glow');
glow.append('feGaussianBlur').attr('stdDeviation','3.5').attr('result','blur');
const merge = glow.append('feMerge');
merge.append('feMergeNode').attr('in','blur');
merge.append('feMergeNode').attr('in','SourceGraphic');

const rg = defs.append('radialGradient').attr('id','bg-glow')
  .attr('cx','50%').attr('cy','50%').attr('r','55%');
rg.append('stop').attr('offset','0%').attr('stop-color','rgba(10,125,140,.07)');
rg.append('stop').attr('offset','100%').attr('stop-color','rgba(6,34,51,0)');
svg.append('rect').attr('width',W).attr('height',H).attr('fill','url(#bg-glow)');

const maxW = d3.max(nodes, d => d.w) || 5;
const rScale    = d3.scaleSqrt().domain([0,maxW]).range([5,26]);
const fontScale = d3.scaleSqrt().domain([0,maxW]).range([8.5,14.5]);
const opScale   = d => 0.5 + (d.w/maxW)*0.5;

// Two-stop teal gradient by weight
const cScale = d3.scaleSequential()
  .domain([0,maxW])
  .interpolator(d3.interpolateRgb('{beacon}','rgba(15,163,181,.95)'));

const sim = d3.forceSimulation(nodes)
  .force('link', d3.forceLink(links).id(d=>d.id)
    .distance(d => 70 - d.w*3).strength(d => Math.min(d.w*0.05,0.35)))
  .force('charge', d3.forceManyBody().strength(d => -100 - rScale(d.w)*9))
  .force('center', d3.forceCenter(W/2, H/2))
  .force('collision', d3.forceCollide().radius(d => rScale(d.w)+20));

const linkSel = svg.append('g').selectAll('line').data(links).join('line')
  .attr('stroke','rgba(15,163,181,.18)')
  .attr('stroke-width', d => Math.min(d.w*0.4+0.2, 2));

const nodeSel = svg.append('g').selectAll('g').data(nodes).join('g')
  .style('cursor','pointer')
  .call(d3.drag()
    .on('start',(e,d)=>{{ if(!e.active) sim.alphaTarget(.3).restart(); d.fx=d.x;d.fy=d.y; }})
    .on('drag', (e,d)=>{{ d.fx=e.x; d.fy=e.y; }})
    .on('end',  (e,d)=>{{ if(!e.active) sim.alphaTarget(0); d.fx=null;d.fy=null; }}));

nodeSel.append('circle')
  .attr('r', d=>rScale(d.w))
  .attr('fill', d=>cScale(d.w))
  .attr('fill-opacity', d=>opScale(d))
  .attr('stroke', d=>cScale(d.w))
  .attr('stroke-width', 1.2)
  .attr('stroke-opacity', 0.6)
  .attr('filter','url(#glow)')
  .on('mouseover', function(e,d){{
    d3.select(this).attr('fill-opacity',1).attr('stroke-opacity',1);
    // highlight connected links
    linkSel
      .attr('stroke', l => (l.source.id===d.id||l.target.id===d.id)
        ? 'rgba(15,163,181,.7)' : 'rgba(15,163,181,.08)')
      .attr('stroke-width', l => (l.source.id===d.id||l.target.id===d.id)
        ? Math.min(l.w*0.6+0.5, 3) : Math.min(l.w*0.4+0.2, 2));
  }})
  .on('mouseout', function(e,d){{
    d3.select(this).attr('fill-opacity',opScale(d)).attr('stroke-opacity',0.6);
    linkSel
      .attr('stroke','rgba(15,163,181,.18)')
      .attr('stroke-width', l => Math.min(l.w*0.4+0.2, 2));
  }});

nodeSel.append('text')
  .attr('class','node-label')
  .text(d=>d.id)
  .attr('text-anchor','middle')
  .attr('dy', d => rScale(d.w) + 13)
  .attr('font-size', d => fontScale(d.w)+'px')
  .attr('fill-opacity', d => 0.6 + (d.w/maxW)*0.4);

sim.on('tick', ()=>{{
  linkSel
    .attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
    .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
  const pad = 32;
  nodeSel.attr('transform', d=>
    `translate(${{Math.max(pad,Math.min(W-pad,d.x))}},${{Math.max(pad,Math.min(H-pad,d.y))}})`
  );
}});
</script>
</body>
</html>"""

    st.components.v1.html(html, height=430, scrolling=False)


# ── Momentum Tracker (A) ──────────────────────────────────────────────────────

def render_momentum_tracker(all_dispatches: list) -> None:
    """Line chart of topic frequency across saved dispatches."""
    import json as _j

    beacon   = CLIENT_BEACON_COLOR
    beacon_2 = CLIENT_BEACON_2

    # Need ≥2 dispatches to show evolution
    if len(all_dispatches) < 2:
        st.markdown(f"""
<div style="background:#062233;border-radius:10px;padding:22px 28px;margin-bottom:4px;
     font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.12em;
     text-transform:uppercase;color:rgba(208,234,240,.45);">
  <span style="color:{beacon_2};font-weight:700;">◈ Momentum Tracker</span>
  &nbsp;·&nbsp; Topic evolution across dispatches
  &nbsp;&nbsp;—&nbsp;&nbsp;
  Requires 2+ saved dispatches · generate more to unlock this view
</div>""", unsafe_allow_html=True)
        return

    # ── Build topic × date matrix ─────────────────────────────────────────────
    from collections import defaultdict
    topic_dates: dict = defaultdict(dict)   # topic -> {date: count}
    all_dates = []

    for rec in reversed(all_dispatches):   # oldest → newest
        date  = rec["timestamp"][:10]
        full  = rec.get("full", {})
        all_dates.append(date)

        # Lead topic_tags (weight 3)
        for t in full.get("lead", {}).get("topic_tags", []):
            t = t.strip().lower()
            topic_dates[t][date] = topic_dates[t].get(date, 0) + 3

        # Card tags (weight 1)
        for card in full.get("cards", []):
            for raw_tag in card.get("tags", "").replace("·", ",").split(","):
                t = raw_tag.strip().lower()
                if t:
                    topic_dates[t][date] = topic_dates[t].get(date, 0) + 1

    all_dates = sorted(set(all_dates))

    # Keep only topics that appear in ≥2 dispatches
    active = {t: v for t, v in topic_dates.items() if len(v) >= 2}
    # Top 8 by total weight
    top8 = sorted(active, key=lambda t: sum(active[t].values()), reverse=True)[:8]

    if not top8:
        return

    # Build series for D3
    series = []
    for t in top8:
        pts = [{"d": date, "v": active[t].get(date, 0)} for date in all_dates]
        series.append({"id": t, "pts": pts})

    series_json = _j.dumps(series)
    dates_json  = _j.dumps(all_dates)

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Fraunces:ital,opsz,wght@0,9..144,500&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{background:#062233;overflow:hidden;width:100%;height:100%;font-family:'JetBrains Mono',monospace;}}
#hdr{{padding:18px 24px 0;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}}
.eye{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:{beacon_2};font-weight:700;}}
.ttl{{font-family:'Fraunces',serif;font-size:16px;font-weight:500;color:#d0eaf0;}}
.sub{{font-size:9.5px;color:rgba(208,234,240,.4);letter-spacing:.06em;text-transform:uppercase;margin-left:auto;}}
#chart{{width:100%;height:320px;}}
.axis path,.axis line{{stroke:rgba(157,196,216,.15);}}
.axis text{{font-family:'JetBrains Mono',monospace;font-size:9px;fill:rgba(208,234,240,.45);}}
.grid line{{stroke:rgba(157,196,216,.07);stroke-dasharray:3,3;}}
.legend{{font-family:'JetBrains Mono',monospace;font-size:9px;fill:rgba(208,234,240,.6);}}
</style></head><body>
<div id="hdr">
  <span class="eye">◈ Momentum Tracker</span>
  <span class="ttl">Topic evolution across dispatches</span>
  <span class="sub">{len(all_dispatches)} dispatches · top {len(top8)} topics</span>
</div>
<svg id="chart"></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const series = {series_json};
const dates  = {dates_json};

const W = document.getElementById('chart').clientWidth || 960;
const H = 320;
const mg = {{top:18,right:130,bottom:36,left:44}};
const iw = W - mg.left - mg.right;
const ih = H - mg.top  - mg.bottom;

const svg = d3.select('#chart').attr('width',W).attr('height',H)
  .append('g').attr('transform',`translate(${{mg.left}},${{mg.top}})`);

const parseDate = d3.timeParse('%Y-%m-%d');
const allDates  = dates.map(parseDate);

const x = d3.scaleTime()
  .domain(d3.extent(allDates)).range([0,iw]);

const maxV = d3.max(series, s => d3.max(s.pts, p => p.v)) || 5;
const y = d3.scaleLinear().domain([0, maxV*1.1]).range([ih,0]);

// Grid
svg.append('g').attr('class','grid')
  .call(d3.axisLeft(y).ticks(4).tickSize(-iw).tickFormat(''));

// Axes
svg.append('g').attr('class','axis').attr('transform',`translate(0,${{ih}})`)
  .call(d3.axisBottom(x).ticks(Math.min(allDates.length,6))
    .tickFormat(d3.timeFormat('%d %b')));
svg.append('g').attr('class','axis')
  .call(d3.axisLeft(y).ticks(4).tickFormat(d=>d||''));

// Colour palette — teal family
const palette = [
  '{beacon_2}','rgba(10,125,140,.9)','rgba(26,138,107,.9)',
  'rgba(110,168,196,.9)','rgba(208,234,240,.7)',
  'rgba(10,74,110,.9)','rgba(15,163,181,.6)','rgba(157,196,216,.8)'
];

const line = d3.line()
  .x(p => x(parseDate(p.d))).y(p => y(p.v))
  .curve(d3.curveCatmullRom.alpha(0.5));

series.forEach((s,i)=>{{
  const col = palette[i % palette.length];
  svg.append('path')
    .datum(s.pts).attr('fill','none')
    .attr('stroke', col).attr('stroke-width',2)
    .attr('stroke-opacity',.85)
    .attr('d', line);

  // Dots
  svg.selectAll(`.dot-${{i}}`).data(s.pts).join('circle')
    .attr('class',`dot-${{i}}`)
    .attr('cx', p => x(parseDate(p.d))).attr('cy', p => y(p.v))
    .attr('r', 3.5).attr('fill', col).attr('fill-opacity',.9);

  // Legend
  const ly = 8 + i * 20;
  svg.append('line')
    .attr('x1',iw+8).attr('x2',iw+22).attr('y1',ly).attr('y2',ly)
    .attr('stroke',col).attr('stroke-width',2);
  svg.append('text').attr('class','legend')
    .attr('x',iw+26).attr('y',ly+4)
    .text(s.id.length > 18 ? s.id.slice(0,17)+'…' : s.id);
}});
</script></body></html>"""

    st.components.v1.html(html, height=370, scrolling=False)


# ── Signal Volume Analytics (1) ───────────────────────────────────────────────

def render_signal_volume(signals: list) -> None:
    """Stacked area chart: signal volume by day and source."""
    import json as _j
    from collections import defaultdict

    beacon   = CLIENT_BEACON_COLOR
    beacon_2 = CLIENT_BEACON_2

    if not signals:
        return

    # Aggregate by day × source
    day_src: dict = defaultdict(lambda: defaultdict(int))
    for s in signals:
        ts  = s.get("timestamp", "")[:10]
        src = s.get("source", "other").lower()
        if ts:
            day_src[ts][src] += 1

    if not day_src:
        return

    all_days    = sorted(day_src.keys())
    all_sources = ["reddit", "tiktok", "rss", "web"]
    src_colors  = {
        "reddit": "#d44800",
        "tiktok": "#0fa3b5",
        "rss":    "#6ea8c4",
        "web":    "#1a8a6b",
    }

    # Build series per source (cumulative for stacking done in D3)
    series = [
        {
            "id":    src,
            "color": src_colors.get(src, "#9dc4d8"),
            "pts":   [{"d": d, "v": day_src[d].get(src, 0)} for d in all_days],
        }
        for src in all_sources
    ]
    series_json = _j.dumps(series)
    days_json   = _j.dumps(all_days)
    total       = len(signals)

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Fraunces:ital,opsz,wght@0,9..144,500&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{background:#062233;overflow:hidden;width:100%;height:100%;font-family:'JetBrains Mono',monospace;}}
#hdr{{padding:18px 24px 4px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}}
.eye{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:{beacon_2};font-weight:700;}}
.ttl{{font-family:'Fraunces',serif;font-size:16px;font-weight:500;color:#d0eaf0;}}
.sub{{font-size:9.5px;color:rgba(208,234,240,.4);letter-spacing:.06em;text-transform:uppercase;margin-left:auto;}}
#chart{{width:100%;height:300px;}}
.axis path,.axis line{{stroke:rgba(157,196,216,.15);}}
.axis text{{font-family:'JetBrains Mono',monospace;font-size:9px;fill:rgba(208,234,240,.45);}}
.grid line{{stroke:rgba(157,196,216,.07);stroke-dasharray:3,3;}}
.legend text{{font-family:'JetBrains Mono',monospace;font-size:9px;fill:rgba(208,234,240,.65);}}
</style></head><body>
<div id="hdr">
  <span class="eye">◉ Signal Volume</span>
  <span class="ttl">Ingestion activity by platform</span>
  <span class="sub">{total:,} signals total</span>
</div>
<svg id="chart"></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const series = {series_json};
const days   = {days_json};

const W  = document.getElementById('chart').clientWidth || 960;
const H  = 300;
const mg = {{top:14, right:110, bottom:36, left:44}};
const iw = W - mg.left - mg.right;
const ih = H - mg.top  - mg.bottom;

const svg = d3.select('#chart').attr('width',W).attr('height',H)
  .append('g').attr('transform',`translate(${{mg.left}},${{mg.top}})`);

const parseDate = d3.timeParse('%Y-%m-%d');
const allDates  = days.map(parseDate);

const x = d3.scaleTime().domain(d3.extent(allDates)).range([0, iw]);

// Stack the data
const stackKeys = series.map(s=>s.id);
const colorMap  = Object.fromEntries(series.map(s=>[s.id, s.color]));
const rows      = days.map((d,i)=>{{
  const row = {{date: parseDate(d)}};
  series.forEach(s=>{{ row[s.id] = s.pts[i]?.v || 0; }});
  return row;
}});

const stack  = d3.stack().keys(stackKeys)(rows);
const maxVal = d3.max(stack, layer => d3.max(layer, d => d[1])) || 10;
const y      = d3.scaleLinear().domain([0, maxVal * 1.05]).range([ih, 0]);

// Grid
svg.append('g').attr('class','grid')
  .call(d3.axisLeft(y).ticks(4).tickSize(-iw).tickFormat(''));

// Axes
svg.append('g').attr('class','axis').attr('transform',`translate(0,${{ih}})`)
  .call(d3.axisBottom(x).ticks(Math.min(days.length, 8)).tickFormat(d3.timeFormat('%d %b')));
svg.append('g').attr('class','axis')
  .call(d3.axisLeft(y).ticks(4));

// Areas
const area = d3.area()
  .x(d => x(d.data.date))
  .y0(d => y(d[0]))
  .y1(d => y(d[1]))
  .curve(d3.curveCatmullRom.alpha(0.5));

svg.selectAll('.layer').data(stack).join('path')
  .attr('class','layer')
  .attr('fill', d => colorMap[d.key])
  .attr('fill-opacity', 0.75)
  .attr('d', area);

// Legend
stack.forEach((layer, i) => {{
  const ly = i * 18;
  svg.append('rect').attr('x', iw+8).attr('y', ly).attr('width', 10).attr('height', 10)
    .attr('fill', colorMap[layer.key]).attr('rx', 2);
  svg.append('text').attr('class','legend')
    .attr('x', iw+22).attr('y', ly+9)
    .text(layer.key.toUpperCase());
}});
</script></body></html>"""

    st.components.v1.html(html, height=350, scrolling=False)


# ── Competitive Pulse (3) ─────────────────────────────────────────────────────

def render_competitive_pulse(signals: list, competitors_str: str) -> None:
    """Track competitor brand mentions across signals by day."""
    import json as _j, re
    from collections import defaultdict

    beacon   = CLIENT_BEACON_COLOR
    beacon_2 = CLIENT_BEACON_2

    competitors = [c.strip() for c in competitors_str.split(",") if c.strip()]
    if not competitors or not signals:
        return

    # Count mentions per day per competitor (case-insensitive word match)
    patterns = {c: re.compile(re.escape(c), re.IGNORECASE) for c in competitors}
    day_comp: dict = defaultdict(lambda: defaultdict(int))
    top_mentions: dict = {c: [] for c in competitors}  # store up to 3 best quotes

    for s in signals:
        ts   = s.get("timestamp", "")[:10]
        text = f"{s.get('title','')} {s.get('content','')}"
        if not ts:
            continue
        for comp, pat in patterns.items():
            if pat.search(text):
                day_comp[ts][comp] += 1
                if len(top_mentions[comp]) < 3:
                    snippet = text[:160].replace('"', "'")
                    top_mentions[comp].append({"src": s.get("source","?"), "text": snippet})

    if not day_comp:
        st.markdown(f"""<div style="background:#062233;border-radius:10px;padding:20px 28px;
            font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.12em;
            text-transform:uppercase;color:rgba(208,234,240,.4);">
          <span style="color:{beacon_2};font-weight:700;">◉ Competitive Pulse</span>
          &nbsp;·&nbsp; No competitor mentions found in current signal database
        </div>""", unsafe_allow_html=True)
        return

    all_days = sorted(day_comp.keys())
    palette  = ["#d44800","#0fa3b5","#6ea8c4","#1a8a6b","#c94f35","#9dc4d8"]

    series = [
        {
            "id":    c,
            "color": palette[i % len(palette)],
            "total": sum(day_comp[d].get(c, 0) for d in all_days),
            "pts":   [{"d": day, "v": day_comp[day].get(c, 0)} for day in all_days],
            "quotes": top_mentions[c],
        }
        for i, c in enumerate(competitors)
        if sum(day_comp[d].get(c, 0) for d in all_days) > 0
    ]
    series.sort(key=lambda s: -s["total"])
    series_json = _j.dumps(series)
    days_json   = _j.dumps(all_days)

    # Build quotes HTML for below chart
    quotes_html = ""
    for s in series[:4]:
        if s["quotes"]:
            q = s["quotes"][0]
            quotes_html += f"""
<div style="border-left:3px solid {s['color']};padding:8px 12px;margin-bottom:8px;background:rgba(255,255,255,.04);border-radius:0 6px 6px 0;">
  <div style="font-size:9px;color:{s['color']};text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px;font-family:'JetBrains Mono',monospace;">{s['id']} · {q['src'].upper()} · {s['total']} mentions</div>
  <div style="font-size:12px;color:rgba(208,234,240,.75);line-height:1.5;">"{q['text']}…"</div>
</div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Fraunces:ital,opsz,wght@0,9..144,500&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{background:#062233;overflow:hidden;width:100%;min-height:100%;font-family:'JetBrains Mono',monospace;}}
#hdr{{padding:18px 24px 4px;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}}
.eye{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:{beacon_2};font-weight:700;}}
.ttl{{font-family:'Fraunces',serif;font-size:16px;font-weight:500;color:#d0eaf0;}}
.sub{{font-size:9.5px;color:rgba(208,234,240,.4);letter-spacing:.06em;text-transform:uppercase;margin-left:auto;}}
#chart{{width:100%;height:240px;}}
#quotes{{padding:8px 24px 16px;}}
.axis path,.axis line{{stroke:rgba(157,196,216,.15);}}
.axis text{{font-family:'JetBrains Mono',monospace;font-size:9px;fill:rgba(208,234,240,.45);}}
.grid line{{stroke:rgba(157,196,216,.07);stroke-dasharray:3,3;}}
.legend text{{font-family:'JetBrains Mono',monospace;font-size:9px;fill:rgba(208,234,240,.65);}}
</style></head><body>
<div id="hdr">
  <span class="eye">◉ Competitive Pulse</span>
  <span class="ttl">Competitor brand mentions in signals</span>
  <span class="sub">{len(signals):,} signals scanned</span>
</div>
<svg id="chart"></svg>
<div id="quotes">{quotes_html}</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const series = {series_json};
const days   = {days_json};

const W  = document.getElementById('chart').clientWidth || 960;
const H  = 240;
const mg = {{top:10, right:130, bottom:36, left:40}};
const iw = W - mg.left - mg.right;
const ih = H - mg.top  - mg.bottom;

const svg = d3.select('#chart').attr('width',W).attr('height',H)
  .append('g').attr('transform',`translate(${{mg.left}},${{mg.top}})`);

const parseDate = d3.timeParse('%Y-%m-%d');
const allDates  = days.map(parseDate);
const x = d3.scaleTime().domain(d3.extent(allDates)).range([0, iw]);

const maxV = d3.max(series, s => d3.max(s.pts, p => p.v)) || 2;
const y    = d3.scaleLinear().domain([0, maxV + 1]).range([ih, 0]);

svg.append('g').attr('class','grid')
  .call(d3.axisLeft(y).ticks(3).tickSize(-iw).tickFormat(''));
svg.append('g').attr('class','axis').attr('transform',`translate(0,${{ih}})`)
  .call(d3.axisBottom(x).ticks(Math.min(days.length,8)).tickFormat(d3.timeFormat('%d %b')));
svg.append('g').attr('class','axis')
  .call(d3.axisLeft(y).ticks(3).tickFormat(d => Math.round(d)));

const line = d3.line()
  .x(p => x(parseDate(p.d))).y(p => y(p.v))
  .curve(d3.curveCatmullRom.alpha(0.5));

series.forEach((s, i) => {{
  svg.append('path').datum(s.pts)
    .attr('fill','none').attr('stroke', s.color)
    .attr('stroke-width', 2).attr('stroke-opacity', .85)
    .attr('d', line);
  svg.selectAll(`.dot-${{i}}`).data(s.pts).join('circle')
    .attr('cx', p => x(parseDate(p.d))).attr('cy', p => y(p.v))
    .attr('r', 3).attr('fill', s.color).attr('fill-opacity', .9);
  // Legend
  const ly = i * 18;
  svg.append('line').attr('x1',iw+8).attr('x2',iw+22)
    .attr('y1',ly+5).attr('y2',ly+5)
    .attr('stroke',s.color).attr('stroke-width',2);
  svg.append('text').attr('class','legend')
    .attr('x',iw+26).attr('y',ly+9)
    .text((s.id.length>18?s.id.slice(0,17)+'…':s.id)+' ('+s.total+')');
}});
</script></body></html>"""

    h = 260 + min(len([s for s in series if s["quotes"]]), 4) * 68
    st.components.v1.html(html, height=h, scrolling=False)


# ── Full HTML for email dispatch (unchanged) ──────────────────────────────────

def build_html(content: dict, signals: list, client: str, tagline: str) -> str:
    sw     = content.get("sweep", {})
    lead   = content.get("lead", {})
    cards  = content.get("cards", [])
    voices = content.get("voices", [])
    provs  = content.get("provocations", [])
    alerts = content.get("alerts", [])

    today_str   = datetime.utcnow().strftime("%A, %d %B %Y")
    vol_no      = f"Vol. I · No. {datetime.utcnow().strftime('%j')}"
    ld          = lead.get("momentum_dir", "up")
    topic_str   = " · ".join(e(t) for t in lead.get("topic_tags", []))
    sig_n       = len(signals)
    sig_display = f"{sig_n/1000:.1f}K" if sig_n < 1_000_000 else f"{sig_n/1_000_000:.2f}M"

    cards_html  = "".join(render_card(c)  for c in cards[:4])
    voices_html = "".join(render_voice(v) for v in voices[:9])
    provs_html  = "".join(render_prov(p)  for p in provs[:3])
    alerts_html = "".join(render_alert(a) for a in alerts[:3])
    src_pills   = sources_pills(signals)
    chips_html  = chip_buttons(lead)

    # Client brand colors (from env)
    beacon      = CLIENT_BEACON_COLOR
    beacon_2    = CLIENT_BEACON_2
    pill_color  = CLIENT_PILL_COLOR
    agency      = e(AGENCY_NAME)

    return f"""<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>The Lighthouse — {e(client)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,400..700&family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet"/>
<style>
:root{{
  /* ── Atlantic Ocean Palette ─────────────────────── */
  --paper:      #ebf2f7;
  --paper-2:    #dce9f2;
  --ink:        #071828;
  --ink-soft:   #274d68;
  --line:       #9dc4d8;
  --line-strong:#6ea8c4;
  --beacon:     {beacon};
  --beacon-2:   {beacon_2};
  --deep:       #062233;
  --atlantic:   #0a4a6e;
  --rising:     #1a8a6b;
  --falling:    #c94f35;
}}
*{{box-sizing:border-box;}} html{{scroll-behavior:smooth;}}
body{{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:'Inter',-apple-system,sans-serif; -webkit-font-smoothing:antialiased;
  background-image:
    radial-gradient(ellipse 80% 40% at 50% -10%, rgba(10,125,140,.08), transparent),
    radial-gradient(ellipse 60% 30% at 90% 110%, rgba(6,34,51,.05), transparent);
}}
.wrap{{max-width:1240px; margin:0 auto; padding:0 28px;}}
.masthead{{border-bottom:3px double var(--ink); padding-top:22px;}}
.masthead-top{{
  display:flex; justify-content:space-between; align-items:flex-end;
  font-family:'JetBrains Mono',monospace; font-size:11px;
  letter-spacing:.06em; text-transform:uppercase; color:var(--ink-soft);
  padding-bottom:14px; border-bottom:1px solid var(--line);
}}
.masthead-top .edition{{display:flex; gap:22px; align-items:center;}}
.live{{display:inline-flex; align-items:center; gap:7px; color:var(--ink); font-weight:700;}}
.live .dot{{width:8px; height:8px; border-radius:50%; background:var(--beacon); animation:pulse 2.4s infinite;}}
@keyframes pulse{{
  0%{{box-shadow:0 0 0 0 rgba(10,125,140,.5);}}
  70%{{box-shadow:0 0 0 10px rgba(10,125,140,0);}}
  100%{{box-shadow:0 0 0 0 rgba(10,125,140,0);}}
}}
.clientbar{{
  display:flex; justify-content:center; align-items:center; gap:10px; padding:12px 0 2px;
  font-family:'JetBrains Mono',monospace; font-size:11px;
  letter-spacing:.18em; text-transform:uppercase; color:var(--ink-soft);
}}
.clientbar .pill{{background:{pill_color}; color:#fff; padding:3px 11px; border-radius:3px; font-weight:700; letter-spacing:.08em;}}
.title-row{{display:flex; align-items:center; justify-content:center; gap:26px; padding:8px 0 10px;}}
.beacon-mark{{position:relative; width:54px; height:54px; flex:none;}}
.beacon-mark .tower{{
  position:absolute; left:50%; bottom:0; transform:translateX(-50%);
  width:14px; height:34px; background:linear-gradient(var(--ink),#1a3d52);
  clip-path:polygon(28% 0,72% 0,100% 100%,0 100%);
}}
.beacon-mark .lamp{{
  position:absolute; left:50%; top:7px; transform:translateX(-50%);
  width:14px; height:11px; background:var(--beacon); border-radius:3px 3px 0 0;
  box-shadow:0 0 16px 4px rgba(10,125,140,.55); z-index:2;
}}
.beacon-mark .beam{{
  position:absolute; left:50%; top:12px; width:0; height:0;
  transform-origin:left center;
  border-top:16px solid transparent; border-bottom:16px solid transparent;
  border-left:64px solid rgba(15,163,181,.28);
  animation:sweep-beam 7s ease-in-out infinite;
}}
@keyframes sweep-beam{{
  0%,100%{{transform:rotate(-32deg); opacity:.2;}}
  50%{{transform:rotate(28deg); opacity:.5;}}
}}
h1.logo{{font-family:'Fraunces',serif; font-weight:600; font-size:58px; letter-spacing:.01em; margin:0; line-height:.95; text-align:center;}}
h1.logo .the{{display:block; font-size:14px; letter-spacing:.42em; font-weight:500; margin-bottom:4px; color:var(--ink-soft);}}
.tagline{{text-align:center; font-family:'Fraunces',serif; font-style:italic; font-size:16px; color:var(--ink-soft); padding:6px 0 16px;}}
.sweep{{display:grid; grid-template-columns:repeat(5,1fr); border-bottom:1px solid var(--line);}}
.sweep .cell{{padding:16px 18px; border-right:1px solid var(--line);}}
.sweep .cell:last-child{{border-right:none;}}
.sweep .k{{font-family:'JetBrains Mono',monospace; font-size:10px; text-transform:uppercase; letter-spacing:.1em; color:var(--ink-soft);}}
.sweep .v{{font-family:'Fraunces',serif; font-size:30px; font-weight:500; margin-top:4px; line-height:1;}}
.sources-line{{display:flex; flex-wrap:wrap; gap:6px; margin-top:7px;}}
.src{{font-family:'JetBrains Mono',monospace; font-size:9.5px; padding:2px 6px; border:1px solid var(--line-strong); border-radius:20px; color:var(--ink-soft); background:var(--paper-2);}}
.src.on{{color:var(--ink); border-color:var(--ink);}}
.src .d{{display:inline-block; width:5px; height:5px; border-radius:50%; background:var(--rising); margin-right:4px; vertical-align:middle;}}
.controls{{display:flex; justify-content:space-between; align-items:center; gap:16px; margin:22px 0 18px; flex-wrap:wrap;}}
.chips{{display:flex; gap:8px; flex-wrap:wrap;}}
.chip{{font-size:12.5px; font-weight:500; padding:7px 14px; border:1px solid var(--line-strong); background:transparent; border-radius:30px; cursor:pointer; color:var(--ink-soft); transition:.15s; font-family:'Inter';}}
.chip:hover{{border-color:var(--ink); color:var(--ink);}}
.chip.active{{background:var(--ink); color:var(--paper); border-color:var(--ink);}}
.section-eyebrow{{font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:var(--beacon); font-weight:700;}}
.grid{{display:grid; grid-template-columns:1fr 326px; gap:34px; padding-bottom:60px;}}
.lead{{border-top:2px solid var(--ink); padding-top:18px; margin-bottom:34px;}}
.lead .meta{{display:flex; gap:14px; align-items:center; font-family:'JetBrains Mono',monospace; font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:var(--ink-soft); margin-bottom:12px; flex-wrap:wrap;}}
.tag-lead{{background:var(--beacon); color:#fff; padding:3px 9px; font-weight:700; border-radius:3px;}}
.lead h2{{font-family:'Fraunces',serif; font-weight:600; font-size:42px; line-height:1.04; margin:6px 0 14px; letter-spacing:-.01em; max-width:19ch;}}
.lead .dek{{font-size:17px; line-height:1.55; color:var(--ink-soft); max-width:62ch; font-family:'Fraunces',serif;}}
.lead-body{{display:grid; grid-template-columns:1.5fr 1fr; gap:28px; margin-top:22px; align-items:start;}}
.pullquote{{border-left:3px solid var(--beacon); padding:4px 0 4px 18px; font-family:'Fraunces',serif; font-style:italic; font-size:18px; line-height:1.45; color:var(--ink);}}
.pullquote cite{{display:block; font-style:normal; font-family:'JetBrains Mono',monospace; font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-soft); margin-top:10px;}}
.signal-stack{{margin-top:18px;}}
.signal{{display:flex; gap:12px; padding:11px 0; border-top:1px solid var(--line); font-size:13px; align-items:baseline;}}
.signal .plat{{font-family:'JetBrains Mono',monospace; font-size:10px; text-transform:uppercase; color:var(--ink-soft); width:66px; flex:none; letter-spacing:.05em;}}
.signal .txt{{line-height:1.4;}}
.signal .num{{margin-left:auto; font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--ink-soft); white-space:nowrap;}}
.counter{{background:var(--deep); color:#d0eaf0; border-radius:8px; padding:20px; position:relative; overflow:hidden;}}
.counter::before{{content:""; position:absolute; top:-40px; right:-40px; width:160px; height:160px; border-radius:50%; background:radial-gradient(circle, rgba(10,125,140,.25), transparent 70%);}}
.counter .lbl{{font-family:'JetBrains Mono',monospace; font-size:10px; letter-spacing:.16em; text-transform:uppercase; color:var(--beacon-2); font-weight:700;}}
.counter h4{{font-family:'Fraunces',serif; font-size:21px; font-weight:600; margin:8px 0 10px; line-height:1.2;}}
.counter p{{font-size:13.5px; line-height:1.5; color:rgba(208,234,240,.75); margin:0 0 16px;}}
.counter .act{{display:flex; gap:8px;}}
.counter button{{flex:1; font-family:'Inter'; font-size:12px; font-weight:600; padding:9px; border-radius:6px; cursor:pointer; border:1px solid rgba(255,255,255,.25); background:transparent; color:#eef5f4; transition:.15s;}}
.counter button.primary{{background:var(--beacon); border-color:var(--beacon); color:#fff;}}
.counter button:hover{{transform:translateY(-1px);}}
.more-eyebrow{{display:flex; align-items:center; gap:14px; margin:8px 0 18px;}}
.more-eyebrow::after{{content:""; flex:1; height:1px; background:var(--line-strong);}}
.cards{{display:grid; grid-template-columns:1fr 1fr; gap:26px 28px;}}
.card{{border-top:1px solid var(--ink); padding-top:14px; cursor:pointer;}}
.card .ctop{{display:flex; justify-content:space-between; align-items:center; margin-bottom:9px;}}
.momentum{{font-family:'JetBrains Mono',monospace; font-size:11px; font-weight:700; display:inline-flex; align-items:center; gap:5px;}}
.momentum.up{{color:var(--rising);}} .momentum.down{{color:var(--falling);}} .momentum.flat{{color:var(--ink-soft);}}
.card .brands{{font-family:'JetBrains Mono',monospace; font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:var(--ink-soft);}}
.card h3{{font-family:'Fraunces',serif; font-weight:600; font-size:23px; line-height:1.1; margin:0 0 9px; transition:.15s;}}
.card:hover h3{{color:var(--beacon);}}
.card p{{font-size:13.5px; line-height:1.5; color:var(--ink-soft); margin:0 0 13px;}}
.spark{{height:34px; display:flex; align-items:flex-end; gap:3px; margin-bottom:11px;}}
.spark i{{flex:1; background:var(--line-strong); border-radius:2px 2px 0 0; display:block; transition:.2s;}}
.card:hover .spark i{{background:var(--beacon-2);}}
.card-foot{{display:flex; justify-content:space-between; align-items:center; font-family:'JetBrains Mono',monospace; font-size:10px; text-transform:uppercase; color:var(--ink-soft); padding-top:9px; border-top:1px dotted var(--line-strong);}}
.reach{{font-weight:700; color:var(--ink);}}
.rail{{display:flex; flex-direction:column; gap:26px;}}
.panel{{border:1px solid var(--line-strong); border-radius:8px; background:rgba(255,255,255,.4);}}
.panel h5{{font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.12em; text-transform:uppercase; margin:0; padding:14px 16px; border-bottom:1px solid var(--line); color:var(--ink); display:flex; justify-content:space-between; align-items:center;}}
.panel h5 .cnt{{color:var(--beacon);}}
.brandrow{{display:flex; align-items:center; gap:12px; padding:11px 16px; border-bottom:1px solid var(--line); cursor:pointer; transition:.12s;}}
.brandrow:last-child{{border-bottom:none;}} .brandrow:hover{{background:var(--paper-2);}}
.brandrow .av{{width:30px; height:30px; border-radius:6px; flex:none; display:grid; place-items:center; font-family:'Fraunces',serif; font-weight:600; font-size:13px; color:#fff;}}
.brandrow .bn{{font-weight:600; font-size:13px; display:flex; align-items:center; gap:6px;}}
.brandrow .bn .ours{{font-family:'JetBrains Mono',monospace; font-size:8px; background:var(--teal); color:#fff; padding:1px 4px; border-radius:3px; letter-spacing:.05em;}}
.brandrow .bi{{font-size:10.5px; color:var(--ink-soft);}}
.brandrow .stat{{margin-left:auto; text-align:right;}}
.brandrow .stat .pct{{font-family:'JetBrains Mono',monospace; font-weight:700; font-size:12px;}}
.brandrow .stat .pct.up{{color:var(--rising);}} .brandrow .stat .pct.down{{color:var(--falling);}}
.brandrow .stat .sub{{font-size:9px; font-family:'JetBrains Mono',monospace; text-transform:uppercase; color:var(--ink-soft);}}
.alert{{padding:13px 16px; border-bottom:1px solid var(--line); display:flex; gap:11px;}}
.alert:last-child{{border-bottom:none;}}
.alert .sev{{width:6px; border-radius:4px; flex:none;}}
.alert .sev.hi{{background:var(--falling);}} .alert .sev.mid{{background:var(--beacon);}} .alert .sev.lo{{background:var(--rising);}}
.alert .atxt{{font-size:13px; line-height:1.4;}} .alert b{{font-weight:600;}}
.alert .atime{{font-family:'JetBrains Mono',monospace; font-size:9.5px; text-transform:uppercase; color:var(--ink-soft); margin-top:4px; letter-spacing:.04em;}}
.digest{{padding:16px;}}
.digest p{{font-family:'Fraunces',serif; font-size:14px; line-height:1.6; color:var(--ink); margin:0 0 14px;}}
.digest .deliver{{display:flex; gap:8px;}}
.digest .deliver button{{flex:1; font-size:11.5px; font-weight:600; padding:9px; border-radius:6px; cursor:pointer; border:1px solid var(--ink); background:var(--ink); color:var(--paper); font-family:'Inter'; transition:.15s;}}
.digest .deliver button.ghost{{background:transparent; color:var(--ink);}}
.digest .deliver button:hover{{opacity:.85;}}
.next{{font-family:'JetBrains Mono',monospace; font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:var(--ink-soft); text-align:center; padding:10px; border-top:1px dashed var(--line-strong);}}
.voices{{margin:0 0 40px;}}
.voices-head{{border-top:2px solid var(--ink); padding-top:16px; margin-bottom:20px;}}
.voices-head .eye{{font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:var(--beacon); font-weight:700;}}
.voices-head h3{{font-family:'Fraunces',serif; font-weight:600; font-size:32px; margin:8px 0 6px; line-height:1.04;}}
.voices-head p{{font-family:'Fraunces',serif; font-style:italic; font-size:15px; color:var(--ink-soft); margin:0; max-width:72ch; line-height:1.5;}}
.voice-grid{{column-count:3; column-gap:22px;}}
.voice{{break-inside:avoid; margin-bottom:22px; background:rgba(255,255,255,.55); border:1px solid var(--line-strong); border-left:3px solid var(--line-strong); border-radius:8px; padding:15px 17px; display:flex; flex-direction:column; gap:11px; transition:.15s;}}
.voice:hover{{transform:translateY(-2px); box-shadow:0 6px 18px rgba(0,0,0,.06);}}
.voice .vtop{{display:flex; justify-content:space-between; align-items:center;}}
.voice .plat{{font-family:'JetBrains Mono',monospace; font-size:10px; text-transform:uppercase; letter-spacing:.07em; font-weight:700;}}
.voice .eng{{font-family:'JetBrains Mono',monospace; font-size:9.5px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink-soft);}}
.voice .q{{font-family:'Fraunces',serif; font-size:16.5px; line-height:1.46; color:var(--ink);}}
.voice .vbot{{display:flex; justify-content:space-between; align-items:center; gap:8px; border-top:1px dotted var(--line-strong); padding-top:9px;}}
.voice .handle{{font-family:'JetBrains Mono',monospace; font-size:10px; color:var(--ink-soft);}}
.voice .rel{{font-family:'JetBrains Mono',monospace; font-size:8.5px; text-transform:uppercase; letter-spacing:.04em; color:#fff; background:var(--ink); padding:2px 6px; border-radius:3px; white-space:nowrap;}}
.p-reddit{{border-left-color:#d93a00;}} .p-reddit .plat{{color:#d93a00;}}
.p-tiktok{{border-left-color:#111;}} .p-tiktok .plat{{color:#111;}}
.p-x{{border-left-color:#111;}} .p-x .plat{{color:#111;}}
.p-mumsnet{{border-left-color:#a4117f;}} .p-mumsnet .plat{{color:#a4117f;}}
.p-ig{{border-left-color:#c13584;}} .p-ig .plat{{color:#c13584;}}
.provocations{{background:var(--deep); color:#d0eaf0; border-radius:10px; padding:34px 34px 30px; margin:0 0 40px; position:relative; overflow:hidden;}}
.provocations::before{{content:""; position:absolute; top:-60px; right:-50px; width:260px; height:260px; border-radius:50%; background:radial-gradient(circle, rgba(10,125,140,.2), transparent 70%);}}
.provocations::after{{content:""; position:absolute; bottom:-70px; left:-40px; width:200px; height:200px; border-radius:50%; background:radial-gradient(circle, rgba(15,163,181,.07), transparent 70%);}}
.prov-head{{display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; margin-bottom:8px; position:relative;}}
.prov-head .eye{{font-family:'JetBrains Mono',monospace; font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:var(--beacon-2); font-weight:700;}}
.prov-head h3{{font-family:'Fraunces',serif; font-weight:600; font-size:30px; margin:0; line-height:1.05;}}
.prov-sub{{font-family:'Fraunces',serif; font-style:italic; font-size:15px; line-height:1.5; color:rgba(208,234,240,.6); margin:0 0 26px; max-width:74ch; position:relative;}}
.prov-grid{{display:grid; grid-template-columns:repeat(3,1fr); gap:26px; position:relative;}}
.prov{{border-top:2px solid rgba(255,255,255,.22); padding-top:15px;}}
.prov .n{{font-family:'Fraunces',serif; font-size:34px; font-weight:300; color:var(--beacon-2); line-height:1; margin-bottom:12px; display:block;}}
.prov p{{font-family:'Fraunces',serif; font-size:18px; line-height:1.4; margin:0 0 14px; color:#f3f8f7;}}
.prov .tag{{font-family:'JetBrains Mono',monospace; font-size:9.5px; text-transform:uppercase; letter-spacing:.06em; color:#9fc0bd;}}
.prov-foot{{display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; margin-top:24px; padding-top:16px; border-top:1px solid rgba(255,255,255,.15); position:relative;}}
.prov-foot span{{font-family:'JetBrains Mono',monospace; font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:#9fc0bd;}}
.prov-foot .btns{{display:flex; gap:8px;}}
.prov-foot button{{font-family:'Inter'; font-size:12px; font-weight:600; padding:9px 16px; border-radius:6px; cursor:pointer; border:1px solid rgba(255,255,255,.3); background:transparent; color:#eef5f4; transition:.15s;}}
.prov-foot button.primary{{background:var(--beacon); border-color:var(--beacon); color:#fff;}}
.prov-foot button:hover{{transform:translateY(-1px);}}
footer{{border-top:3px double var(--ink); padding:22px 0 40px; font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--ink-soft); text-transform:uppercase; letter-spacing:.06em; display:flex; justify-content:space-between; flex-wrap:wrap; gap:12px;}}
@media(max-width:1000px){{
  .prov-grid,.grid,.lead-body,.cards{{grid-template-columns:1fr;}}
  .sweep{{grid-template-columns:repeat(2,1fr);}}
  .voice-grid{{column-count:1;}}
  h1.logo{{font-size:44px;}}
  .lead h2{{font-size:31px;}}
}}
@media print{{
  @page{{margin:18mm 16mm;}}
  body{{background:var(--paper)!important;-webkit-print-color-adjust:exact;print-color-adjust:exact;}}
  .agency-bar{{background:var(--ink)!important;}}
  .provocations,.counter{{background:var(--deep)!important;}}
  .design-badge,.controls button,.prov-foot .btns,.digest .deliver,.card-foot button{{display:none!important;}}
  .grid{{grid-template-columns:1fr!important;}}
  .prov-grid{{grid-template-columns:repeat(3,1fr)!important;}}
  .voice-grid{{column-count:2!important;}}
  .lead h2{{font-size:28px;}}
  h1.logo{{font-size:52px;}}
  .voices,.cards,.provocations{{page-break-before:always;}}
  a{{text-decoration:none;color:inherit;}}
}}
</style>
</head>
<body>
<div class="wrap">

  <div style="background:var(--ink);color:rgba(255,255,255,.45);font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;display:flex;justify-content:space-between;align-items:center;padding:7px 28px;">
    <span>Cultural Intelligence Platform · Powered by Countercurrent</span>
    <span style="color:#fff;font-weight:700;letter-spacing:.22em;">{agency}</span>
  </div>
  <header class="masthead">
    <div class="masthead-top">
      <div class="edition"><span>{vol_no}</span><span>{today_str}</span></div>
      <div class="edition">
        <span class="live"><span class="dot"></span> Sweeping live</span>
        <span>Leadership Edition</span>
      </div>
    </div>
    <div class="clientbar"><span class="pill">Client</span> {e(client)}</div>
    <div class="title-row">
      <div class="beacon-mark">
        <span class="beam"></span><span class="lamp"></span><span class="tower"></span>
      </div>
      <h1 class="logo"><span class="the">THE</span>LIGHTHOUSE</h1>
    </div>
    <p class="tagline">{e(tagline)}</p>
  </header>

  <section class="sweep">
    <div class="cell">
      <div class="k">Signals scanned · 24h</div>
      <div class="v" id="sigcount">{sig_display}</div>
    </div>
    <div class="cell"><div class="k">Currents surfaced</div><div class="v">{sw.get("currents_surfaced","—")}</div></div>
    <div class="cell"><div class="k">Rising fast</div><div class="v" style="color:var(--rising)">{sw.get("rising_fast","—")}</div></div>
    <div class="cell"><div class="k">Needs a human</div><div class="v" style="color:var(--beacon)">{sw.get("needs_human","—")}</div></div>
    <div class="cell"><div class="k">Sources active</div><div class="sources-line">{src_pills}</div></div>
  </section>

  <div class="controls">
    <div class="chips">
      <button class="chip active">All currents</button>
      {chips_html}
    </div>
    <div class="section-eyebrow">▲ Today's strongest current</div>
  </div>

  <div class="grid">
    <main>
      <article class="lead">
        <div class="meta">
          <span class="tag-lead">Lead Current</span>
          <span>{topic_str}</span>
          <span>Relevance: <b>{e(lead.get("relevance","—"))}</b></span>
          <span class="momentum {_mclass(ld)}">{_marrow(ld)} {e(lead.get("momentum_pct",""))}/{e(lead.get("momentum_period",""))}</span>
        </div>
        <h2>{e(lead.get("title",""))}</h2>
        <p class="dek">{e(lead.get("dek",""))}</p>
        <div class="lead-body">
          <div>
            <div class="pullquote">
              &ldquo;{e(lead.get("pullquote",""))}&rdquo;
              <cite>— {e(lead.get("pullquote_cite",""))}</cite>
            </div>
            {render_signal_stack(lead.get("signal_stack",[]))}
          </div>
          <aside class="counter">
            <div class="lbl">◐ The Countercurrent</div>
            <h4>{e(lead.get("countercurrent_title",""))}</h4>
            <p>{e(lead.get("countercurrent_body",""))}</p>
            <div class="act">
              <button class="primary">Brief the team →</button>
              <button>Save</button>
            </div>
          </aside>
        </div>
      </article>

      <div class="more-eyebrow">
        <span class="section-eyebrow">More currents worth watching</span>
      </div>
      <div class="cards">{cards_html}</div>
    </main>

    <aside class="rail">
      <div class="panel">
        <h5>Share Of Voice <span class="cnt">7d</span></h5>
        <div class="brandrow">
          <div class="av" style="background:#0e9aa7">H</div>
          <div><div class="bn">Heinz Cream of Tomato <span class="ours">OURS</span></div><div class="bi">Can · flagship</div></div>
          <div class="stat"><div class="pct up">▲ 41%</div><div class="sub">Conversation</div></div>
        </div>
        <div class="brandrow">
          <div class="av" style="background:#cf2b29">H</div>
          <div><div class="bn">Heinz Soup of the Day <span class="ours">OURS</span></div><div class="bi">Pouch · convenience</div></div>
          <div class="stat"><div class="pct up">▲ 63%</div><div class="sub">Conversation</div></div>
        </div>
        <div class="brandrow">
          <div class="av" style="background:#3a6e3a">C</div>
          <div><div class="bn">Cully &amp; Sully</div><div class="bi">Pot · competitor</div></div>
          <div class="stat"><div class="pct up">▲ 28%</div><div class="sub">Gaining</div></div>
        </div>
        <div class="brandrow">
          <div class="av" style="background:#6b4e8c">G</div>
          <div><div class="bn">New Covent Garden</div><div class="bi">Carton · competitor</div></div>
          <div class="stat"><div class="pct" style="color:var(--ink-soft)">● 2%</div><div class="sub">Flat</div></div>
        </div>
        <div class="brandrow">
          <div class="av" style="background:#a9572b">B</div>
          <div><div class="bn">Batchelors Cup-a-Soup</div><div class="bi">Sachet · competitor</div></div>
          <div class="stat"><div class="pct down">▼ 19%</div><div class="sub">Declining</div></div>
        </div>
      </div>

      <div class="panel">
        <h5>Needs A Human <span class="cnt">{len(alerts)} open</span></h5>
        {alerts_html}
      </div>

      <div class="panel">
        <h5>The 07:00 Briefing</h5>
        <div class="digest">
          <p>&ldquo;{e(content.get("briefing",""))}&rdquo;</p>
          <div class="deliver">
            <button>Send to leads</button>
            <button class="ghost">Full report</button>
          </div>
        </div>
        <div class="next">◷ Next sweep on demand</div>
      </div>
    </aside>
  </div>

  <section class="voices">
    <div class="voices-head">
      <span class="eye">● Live · Voices From The Currents</span>
      <h3>What people are actually saying</h3>
      <p>Raw signal texture from this sweep — the language, jokes and feelings real people attach to the category. Steal the language.</p>
    </div>
    <div class="voice-grid">{voices_html}</div>
  </section>

  <section class="provocations">
    <div class="prov-head">
      <span class="eye">◐ To Close · The Countercurrent</span>
      <h3>Three provocations for the room</h3>
    </div>
    <p class="prov-sub">Deliberately unfinished questions drawn from today's currents — not answers, but opening lines to push the team past the obvious. Argue with these.</p>
    <div class="prov-grid">{provs_html}</div>
    <div class="prov-foot">
      <span>Generated fresh from today's strongest currents</span>
      <div class="btns">
        <button class="primary">Send to creative team →</button>
        <button>Regenerate</button>
      </div>
    </div>
  </section>

  <footer>
    <span>The Lighthouse · Countercurrent.ai v3</span>
    <span style="color:var(--beacon);font-weight:700;letter-spacing:.14em;">{agency}</span>
    <span>Client: {e(client)} · Refreshes on demand · Human-reviewed before send</span>
  </footer>

</div>
<script>
(function() {{
  // Chip filter toggle
  document.querySelectorAll('.chip').forEach(function(c) {{
    c.addEventListener('click', function() {{
      document.querySelectorAll('.chip').forEach(function(x) {{ x.classList.remove('active'); }});
      c.classList.add('active');
    }});
  }});
  // Animate signal counter
  var el = document.getElementById('sigcount');
  var base = {sig_n};
  if (el && base > 0) {{
    setInterval(function() {{
      base += Math.floor(Math.random() * 4 + 1);
      el.textContent = base >= 1000000
        ? (base / 1000000).toFixed(2) + 'M'
        : (base / 1000).toFixed(1) + 'K';
    }}, 1800);
  }}
}})();
</script>
</body>
</html>"""


# ── Session state ──────────────────────────────────────────────────────────────

if "lh_content" not in st.session_state:
    st.session_state.lh_content = None


# ── Load data ──────────────────────────────────────────────────────────────────

signals = load_signals()

# ── Sidebar signal health indicator (fills placeholder set above) ──────────────
if not IS_CLIENT:
    if signals:
        _sig_count_placeholder.caption(f"📡 {len(signals)} signals loaded")
    else:
        _sig_count_placeholder.warning("⚠️ No signals — run ingestion first")


def load_last_dispatch(path: str = "data/dispatches.jsonl"):
    """Load the most recent saved dispatch. Delegates to db.py."""
    rec = _db.load_last_dispatch()
    if rec is None:
        return None
    full = rec.get("full") or {}
    full["_dispatch_id"] = rec.get("dispatch_id") or rec.get("timestamp", "fallback")
    return full




# ── Mode: saved (free) vs live (calls Claude) ──────────────────────────────────

if not live_mode:
    # SAVED MODE — load last dispatch, zero API cost
    if st.session_state.lh_content is None:
        saved = load_last_dispatch()
        if saved:
            st.session_state.lh_content = saved
            st.sidebar.caption("Showing last saved dispatch.")
        else:
            st.session_state.lh_content = _fallback()
            st.sidebar.caption("No dispatch saved yet — switch to Live to generate.")

elif regenerate:
    # LIVE MODE — call Claude
    if not signals:
        st.warning(
            "⚠️ No signals in the database. "
            "Run ingestion (e.g. `python ingestion_ny_liberty.py`) to populate signals, "
            "then try Sweep & Generate again.",
            icon="📡",
        )
        st.session_state.lh_content = _fallback()
    else:
        try:
            with st.spinner("🗼 The Lighthouse is sweeping the currents…"):
                rag = []
                if use_pinecone and focus_topic:
                    rag = semantic_search(
                        focus_topic,
                        top_k=signal_limit,
                        client_filter=client_filter or None,
                    )
                content = generate(signals, rag, client_name, brief_tagline, focus_topic)
                st.session_state.lh_content = content
                save_dispatch(content, focus_topic)
                # Record sweep run for velocity tracking
                _db.record_sweep_run(
                    topic=focus_topic or "general",
                    signal_count=len(signals),
                    sources=["live"],
                )
        except Exception as _sweep_exc:
            st.error(f"Sweep failed: {_sweep_exc}")

content = st.session_state.lh_content


# ── Email dispatch ─────────────────────────────────────────────────────────────

if send_email_btn and email_to and content:
    html_body  = build_html(content, signals, client_name, brief_tagline)
    week_label = datetime.utcnow().strftime("Week of %d %b %Y")
    subject    = f"The Lighthouse · {client_name} · {week_label}"
    ok = send_email(email_to, subject, html_body)
    if ok:
        st.toast(f"✓ Dispatch sent to {email_to}")
    else:
        st.error("Send failed — check SENDGRID_API_KEY and SENDGRID_FROM_EMAIL in .env")


# ── Fill report download button (load_signals + build_html now in scope) ───────

if _has_content:
    _report_html = build_html(
        st.session_state["lh_content"], load_signals(),
        client_name, brief_tagline,
    )
    _download_placeholder.download_button(
        "↓ Download Report (HTML→PDF)",
        data=_report_html,
        file_name=f"lighthouse_{_date_str}.html",
        mime="text/html",
        use_container_width=True,
        help="Open in browser → Print → Save as PDF",
    )
else:
    _download_placeholder.caption("Generate a dispatch first to download the report.")


# ── Footer (factored out so both the client view and the full page can use it) ─

def render_footer():
    st.markdown(f"""
<div style="border-top:3px double #071828;margin-top:3rem;padding:24px 0 48px;
     font-family:'JetBrains Mono',monospace;font-size:11px;color:#274d68;
     text-transform:uppercase;letter-spacing:.06em;
     display:flex;justify-content:space-between;flex-wrap:wrap;gap:14px;">
  <span>The Lighthouse · Countercurrent.ai v3</span>
  <span style="color:{CLIENT_BEACON_COLOR};font-weight:700;letter-spacing:.14em">{e(AGENCY_NAME)}</span>
  <span>Refreshes on demand · Human-reviewed before send</span>
</div>
""", unsafe_allow_html=True)


# ── Top-level navigation: Trends / Dispatch / Projects / Road Map ──────────
# One-page layout: hero masthead always visible above the nav bar.
# Clients see masthead + dispatch content only (no nav bar, no other sections).
# Internal team sees the full 4-section one-page layout.
# We enter/exit tabs via the DeltaGenerator __enter__/__exit__ protocol so
# existing render blocks don't need to be re-indented.

# ── Hero masthead — always visible, above all tabs ─────────────────────────
if content:
    current_user = st.session_state.get("logged_in_user", "internal")
    masthead_html = build_masthead_html(content, signals, client_name, brief_tagline)
    st.components.v1.html(masthead_html, height=510, scrolling=False)
else:
    current_user = st.session_state.get("logged_in_user", "internal")

# ── Clients: dispatch content only, then stop ──────────────────────────────
if IS_CLIENT:
    if content:
        st.markdown('<div id="lh-sec-lead"></div>', unsafe_allow_html=True)
        render_content_sections(content, current_user, show_competitive=_has_perm("competitive_pulse"))
    else:
        st.info("No dispatch available yet. Please check back soon.")
    render_footer()
    st.stop()

# ── Internal nav bar (non-clients) ─────────────────────────────────────────
# Invisible marker lets the CSS below target only THIS st.tabs() —
# nested tab bars elsewhere (My Board, Briefing Builder, etc.) keep default look.
st.markdown('<div id="lh-toptabs-marker" style="display:none"></div>', unsafe_allow_html=True)
tab_feed, tab_trends, tab_dispatch, tab_projects, tab_roadmap = st.tabs([
    "⚡ Feed", "Trends", "Dispatch", "Projects", "Road Map",
])

# ══════════════════════════════════════════════════════════════════════════════
# FEED TAB — simplified signal scanner (first tab, MVP view)
# One prompt → live search across all sources → raw signal cards + save.
# No AI synthesis, no verdicts — just the currents, surfaced fast.
# ══════════════════════════════════════════════════════════════════════════════
tab_feed.__enter__()

st.markdown("""
<style>
/* ── Feed tab shell ── */
.fd-header {padding: 2rem 0 1.2rem; border-top: 3px double #071828; margin-top: 0.5rem;}
.fd-eyebrow {font-family:'JetBrains Mono',monospace; font-size:10px; letter-spacing:.18em;
  text-transform:uppercase; color:#0a7d8c; font-weight:700; margin-bottom:6px;}
.fd-title {font-family:Georgia,serif; font-size:1.6rem; font-weight:400; color:#071828; margin-bottom:4px;}
.fd-sub {font-size:13px; color:#6ea8c4;}
/* signal cards */
.fd-card {border:1px solid #d0e4ed; border-radius:10px; padding:0; overflow:hidden;
  background:#fff; transition:.2s; margin-bottom:0;}
.fd-card:hover {border-color:#0fa3b5; box-shadow:0 2px 12px rgba(15,163,181,.12);}
.fd-thumb-wrap {height:130px; background:linear-gradient(135deg,#1a3d52,#0fa3b5);
  position:relative; overflow:hidden;}
.fd-thumb-icon {position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;color:rgba(255,255,255,.3);font-size:28px;}
.fd-card-body {padding:10px 12px 12px;}
.fd-src-badge {display:inline-block;font-family:'JetBrains Mono',monospace;font-size:8.5px;
  letter-spacing:.08em;text-transform:uppercase;padding:2px 7px;border-radius:20px;
  background:#e4f4f5;color:#0a7d8c;border:1px solid #b0dde4;margin-bottom:7px;}
.fd-card-title {font-family:Georgia,serif;font-size:13.5px;font-weight:600;
  color:#071828;line-height:1.4;margin-bottom:6px;}
.fd-excerpt {font-size:11.5px;color:#476a7a;line-height:1.5;margin-bottom:8px;}
.fd-foot {font-family:'JetBrains Mono',monospace;font-size:9.5px;color:#9dc4d8;}
/* synopsis box */
.fd-synopsis {background:#e4f4f5;border-left:3px solid #0fa3b5;border-radius:0 8px 8px 0;
  padding:12px 16px;margin:1rem 0;font-size:13px;color:#071828;line-height:1.6;}
.fd-synopsis b {font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.1em;
  text-transform:uppercase;color:#0a7d8c;display:block;margin-bottom:4px;}
/* empty state */
.fd-empty {text-align:center;padding:4rem 2rem;color:#9dc4d8;}
.fd-empty-icon {font-size:2rem;margin-bottom:1rem;}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="fd-header">
  <div class="fd-eyebrow">◎ Signal Feed</div>
  <div class="fd-title">What are you monitoring?</div>
  <div class="fd-sub">Enter a brand, competitor, topic or question — the feed scans the internet and returns raw signals.</div>
</div>
""", unsafe_allow_html=True)

# ── Feed: input row ─────────────────────────────────────────────────────────
_fd_col_in, _fd_col_btn = st.columns([5, 1])
with _fd_col_in:
    _fd_query = st.text_input(
        "Feed query",
        placeholder="e.g. Heinz, desk lunch culture, Adidas football boots…",
        label_visibility="collapsed",
        key="fd_query",
    )
with _fd_col_btn:
    _fd_run = st.button("Search →", key="fd_run", use_container_width=True, type="primary")

# ── Feed: source selector ───────────────────────────────────────────────────
_fd_src_options = ["Reddit", "YouTube", "RSS", "GDELT", "Hacker News", "TikTok", "Instagram", "X/Twitter", "Web (Firecrawl)"]
with st.expander("Sources", expanded=False):
    _fd_src_cols = st.columns(4)
    _fd_sources_sel = []
    for _i, _s in enumerate(_fd_src_options):
        with _fd_src_cols[_i % 4]:
            if st.checkbox(_s, value=True, key=f"fd_src_{_s}"):
                _fd_sources_sel.append(_s)

# ── Feed: run search ────────────────────────────────────────────────────────
if _fd_run and _fd_query.strip():
    _fd_api_key        = os.environ.get("ANTHROPIC_API_KEY", "")
    _fd_apify_key      = os.environ.get("APIFY_API_TOKEN", "")
    _fd_youtube_key    = os.environ.get("YOUTUBE_API_KEY", "")
    _fd_firecrawl_key  = os.environ.get("FIRECRAWL_API_KEY", "")

    # Keyword extraction (same logic as Evidence block)
    _fd_stops = {
        "the","and","but","for","at","their","to","of","in","a","an","is","are",
        "was","were","be","been","have","has","had","do","does","did","will","would",
        "could","should","may","might","i","you","he","she","it","we","they","what",
        "which","who","when","where","why","how","all","so","than","too","very","just",
        "as","if","by","or","also","with","from","on","off","over","under","again",
        "then","here","there","not","no","only","same","such","even","into","about",
        "than","people","avoid","fear","this","that",
    }
    _fd_kws = [
        w for w in _re_global.sub(r"[^\w\s]", "", _fd_query.lower()).split()
        if len(w) > 2 and w not in _fd_stops
    ][:4]
    _fd_search = " ".join(_fd_kws) if _fd_kws else _fd_query[:60]

    _fd_status = st.empty()

    def _fd_set_status(msg: str) -> None:
        _fd_status.markdown(
            f'<style>'
            f'@keyframes _fdblink{{0%,100%{{opacity:.15;}}50%{{opacity:1;}}}}'
            f'._fdd1{{display:inline-block;animation:_fdblink 1.2s 0s infinite;}}'
            f'._fdd2{{display:inline-block;animation:_fdblink 1.2s .4s infinite;}}'
            f'._fdd3{{display:inline-block;animation:_fdblink 1.2s .8s infinite;}}'
            f'</style>'
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:11.5px;'
            f'color:#0a5560;background:#e4f4f5;border-left:3px solid #0fa3b5;'
            f'border-radius:0 6px 6px 0;padding:7px 12px;margin:4px 0;">'
            f'{msg}'
            f'<span class="_fdd1">.</span><span class="_fdd2">.</span><span class="_fdd3">.</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    _fd_raw: list[dict] = []

    try:
        from ingestion import (scrape_reddit, scrape_rss, scrape_gdelt,
                               scrape_hacker_news, scrape_youtube,
                               scrape_tiktok, scrape_instagram, scrape_twitter)

        def _fd_cb(msg): _fd_set_status(msg)

        if "Reddit" in _fd_sources_sel:
            _fd_set_status(f"[Reddit] Searching '{_fd_search}'")
            for s in scrape_reddit(_fd_search, max_items=12, callback=_fd_cb):
                _fd_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": "reddit", "url": s.url, "timestamp": s.timestamp})

        if "RSS" in _fd_sources_sel:
            _fd_set_status("Scanning RSS feeds")
            for s in scrape_rss(max_items_per_feed=3, callback=_fd_cb):
                _fd_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": "rss", "url": s.url, "timestamp": s.timestamp})

        if "GDELT" in _fd_sources_sel:
            _fd_set_status(f"[GDELT] Searching '{_fd_search}'")
            for s in scrape_gdelt(_fd_search, n=12, callback=_fd_cb):
                _fd_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": "gdelt", "url": s.url, "timestamp": s.timestamp})

        if "Hacker News" in _fd_sources_sel:
            _fd_set_status(f"[Hacker News] Searching '{_fd_search}'")
            for s in scrape_hacker_news(_fd_search, n=8, callback=_fd_cb):
                _fd_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": "hacker_news", "url": s.url, "timestamp": s.timestamp})

        if "YouTube" in _fd_sources_sel and _fd_youtube_key:
            _fd_yt_terms = _fd_kws[:3] if len(_fd_kws) > 1 else [_fd_search]
            _fd_yt_seen: set = set()
            for _fd_yt_kw in _fd_yt_terms:
                _fd_set_status(f"[YouTube] Searching '{_fd_yt_kw}'")
                for s in scrape_youtube(_fd_yt_kw, api_key=_fd_youtube_key,
                                        n=5, region_code="GB", callback=_fd_cb):
                    if s.url not in _fd_yt_seen:
                        _fd_yt_seen.add(s.url)
                        _fd_raw.append({"title": s.title, "content": s.content,
                                        "thumbnail": (s.raw_meta or {}).get("thumbnail", ""),
                                        "source": "youtube", "url": s.url, "timestamp": s.timestamp})

        if "TikTok" in _fd_sources_sel and _fd_apify_key:
            _fd_set_status(f"[TikTok] Searching '{_fd_search}' via Apify")
            for s in scrape_tiktok(_fd_search, api_token=_fd_apify_key, n=12,
                                   fetch_comments=False, callback=_fd_cb):
                _fd_raw.append({"title": s.title, "content": s.content,
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", ""),
                                "source": "tiktok", "url": s.url, "timestamp": s.timestamp})

        if "Instagram" in _fd_sources_sel and _fd_apify_key:
            _fd_set_status(f"[Instagram] Searching '{_fd_search}' via Apify")
            for s in scrape_instagram(_fd_search, api_token=_fd_apify_key, n=12, callback=_fd_cb):
                _fd_raw.append({"title": s.title, "content": s.content,
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", ""),
                                "source": "instagram", "url": s.url, "timestamp": s.timestamp})

        if "X/Twitter" in _fd_sources_sel and _fd_apify_key:
            _fd_twitter_errors: list[str] = []
            def _fd_tw_cb(msg: str):
                _fd_set_status(msg)
                if "error" in msg.lower() or "Error" in msg:
                    _fd_twitter_errors.append(msg)
            _fd_tw_before = len(_fd_raw)
            for s in scrape_twitter(_fd_search, api_token=_fd_apify_key, n=12, callback=_fd_tw_cb):
                _fd_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": "twitter", "url": s.url, "timestamp": s.timestamp})
            if _fd_twitter_errors and len(_fd_raw) == _fd_tw_before:
                st.warning("X/Twitter: " + " · ".join(_fd_twitter_errors))

        if "Web (Firecrawl)" in _fd_sources_sel and _fd_firecrawl_key:
            from ingestion import scrape_web as _scrape_web_fd
            _fd_set_status(f"[Web] Searching '{_fd_search}' via Firecrawl")
            for s in _scrape_web_fd(_fd_search, api_key=_fd_firecrawl_key, n=10, callback=_fd_cb):
                _fd_raw.append({"title": s.title, "content": s.content,
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", ""),
                                "source": "web", "url": s.url, "timestamp": s.timestamp})
        elif "Web (Firecrawl)" in _fd_sources_sel and not _fd_firecrawl_key:
            st.caption("⚠️ FIRECRAWL_API_KEY not set — add it to Streamlit secrets to enable Web search.")

    except Exception as _fd_scrape_err:
        st.warning(f"Some sources failed: {_fd_scrape_err}")

    # ── Optional: brief synopsis from Claude ──────────────────────────────
    _fd_synopsis = ""
    if _fd_raw and _fd_api_key:
        try:
            _fd_set_status(f"Claude reading {len(_fd_raw)} signals")
            import anthropic as _ant_fd
            _fd_client = _ant_fd.Anthropic(api_key=_fd_api_key)
            _fd_titles = "\n".join(f"- {s.get('title','')[:100]}" for s in _fd_raw[:20])
            _fd_synopsis_resp = _fd_client.messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
                max_tokens=120,
                messages=[{"role": "user", "content":
                    f"You are a concise cultural analyst. In 1-2 sentences (max 40 words total), "
                    f"describe what these signals collectively suggest about '{_fd_query}'. "
                    f"Be factual and sharp. No recommendations.\n\nSignals:\n{_fd_titles}"}],
            )
            _fd_synopsis = _fd_synopsis_resp.content[0].text.strip()
        except Exception:
            pass

    _fd_status.empty()
    st.session_state["fd_results"] = _fd_raw
    st.session_state["fd_query_used"] = _fd_query
    st.session_state["fd_synopsis"] = _fd_synopsis
    st.rerun()

elif _fd_run and not _fd_query.strip():
    st.warning("Enter a search query first.")

# ── Feed: display results ────────────────────────────────────────────────────
_fd_stored = st.session_state.get("fd_results", [])
_fd_q_stored = st.session_state.get("fd_query_used", "")
_fd_syn_stored = st.session_state.get("fd_synopsis", "")

if _fd_stored:
    st.caption(f"**{len(_fd_stored)} signals** · query: *{_fd_q_stored}*")

    if _fd_syn_stored:
        st.markdown(
            f'<div class="fd-synopsis"><b>Synopsis</b>{e(_fd_syn_stored)}</div>',
            unsafe_allow_html=True,
        )

    # ── Source filter ─────────────────────────────────────────────────────
    _fd_src_present = sorted({r.get("source","?") for r in _fd_stored})
    _fd_src_labels = {"reddit":"Reddit","rss":"RSS","gdelt":"GDELT","hacker_news":"HN",
                      "youtube":"YouTube","tiktok":"TikTok","instagram":"Instagram",
                      "twitter":"X/Twitter","web":"Web"}
    _fd_filter_opts = ["All"] + [_fd_src_labels.get(s, s.title()) for s in _fd_src_present]
    _fd_filter = st.radio("Filter by source", _fd_filter_opts, horizontal=True,
                          label_visibility="collapsed", key="fd_filter")
    _fd_filter_key = {v: k for k, v in _fd_src_labels.items()}.get(_fd_filter, "")
    _fd_show = _fd_stored if _fd_filter == "All" else [r for r in _fd_stored if r.get("source") == _fd_filter_key]

    st.markdown("---")

    # ── 3-column card grid ────────────────────────────────────────────────
    _fd_cols_per_row = 3
    for _fd_row_start in range(0, len(_fd_show), _fd_cols_per_row):
        _fd_row_items = _fd_show[_fd_row_start:_fd_row_start + _fd_cols_per_row]
        _fd_row_cols = st.columns(_fd_cols_per_row)
        for _fd_ci, _fd_r in enumerate(_fd_row_items):
            with _fd_row_cols[_fd_ci]:
                _fd_src   = _fd_r.get("source", "?")
                _fd_title = _fd_r.get("title", "")[:100]
                _fd_body  = _fd_r.get("content", "")[:200]
                _fd_url   = _fd_r.get("url", "")
                _fd_ts    = (_fd_r.get("timestamp") or "")[:10]
                _fd_thumb = _fd_r.get("thumbnail", "") or ""
                _fd_src_lbl = _fd_src_labels.get(_fd_src, _fd_src.title())

                # Thumbnail
                if _fd_thumb:
                    _fd_prx = _tr_proxy_thumb(_fd_thumb)
                    _fd_is_social = _fd_src in ("instagram", "tiktok")
                    _fd_grad = ("linear-gradient(135deg,#6a1f6e,#c94f35,#e8a020)"
                                if _fd_is_social else "linear-gradient(135deg,#1a3d52,#0fa3b5)")
                    _fd_thumb_html = (
                        f'<div class="fd-thumb-wrap" style="background:{_fd_grad};">'
                        f'<div class="fd-thumb-icon">✦</div>'
                        f'<img src="{_fd_prx}" loading="lazy" '
                        f'style="position:absolute;inset:0;width:100%;height:100%;'
                        f'object-fit:cover;opacity:0;transition:opacity .4s;" '
                        f'onload="this.style.opacity=1" '
                        f'onerror="this.style.display=\'none\'"/>'
                        f'</div>'
                    )
                else:
                    _fd_thumb_html = ""

                st.markdown(f"""
<div class="fd-card">
  {_fd_thumb_html}
  <div class="fd-card-body">
    <span class="fd-src-badge">{e(_fd_src_lbl)}</span>
    <div class="fd-card-title">{e(_fd_title)}</div>
    <div class="fd-excerpt">{e(_fd_body)}{"…" if len(_fd_r.get("content","")) > 200 else ""}</div>
    <div class="fd-foot">{e(_fd_ts)}</div>
  </div>
</div>""", unsafe_allow_html=True)

                _fd_btn_cols = st.columns([3, 1])
                with _fd_btn_cols[0]:
                    if _fd_url:
                        st.markdown(f'<a href="{_fd_url}" target="_blank" style="font-size:11px;color:#0fa3b5;">Open ↗</a>',
                                    unsafe_allow_html=True)
                with _fd_btn_cols[1]:
                    if st.button("+ Save", key=f"fd_save_{_fd_row_start}_{_fd_ci}",
                                 use_container_width=True):
                        _fd_u = st.session_state.get("logged_in_user", "internal")
                        add_curadoria_item(
                            _fd_u, _fd_src, _fd_title,
                            _fd_r.get("content", "")[:1000],
                        )
                        st.success("Saved!")

else:
    st.markdown("""
<div class="fd-empty">
  <div class="fd-empty-icon">⚡</div>
  <div style="font-size:14px;font-family:Georgia,serif;margin-bottom:8px;">
    Type a topic above and press <em>Search →</em>
  </div>
  <div style="font-size:12px;font-family:monospace;letter-spacing:.06em;text-transform:uppercase;">
    Scans Reddit · YouTube · RSS · GDELT · TikTok · Instagram · X/Twitter
  </div>
</div>""", unsafe_allow_html=True)

tab_feed.__exit__(None, None, None)

# ── Dispatch tab content (editorial intelligence) ──────────────────────────
tab_dispatch.__enter__()

if content:
    st.markdown('<div id="lh-sec-lead"></div>', unsafe_allow_html=True)
    render_content_sections(content, current_user, show_competitive=_has_perm("competitive_pulse"))
    if _has_perm("topic_map"):
        st.markdown('<div id="lh-sec-topicmap"></div>', unsafe_allow_html=True)
        render_topic_map(content)
    if _has_perm("momentum"):
        _all_disp = load_all_dispatches()
        st.markdown('<div id="lh-sec-momentum"></div>', unsafe_allow_html=True)
        render_momentum_tracker(_all_disp)
    if _has_perm("signal_volume"):
        st.markdown('<div id="lh-sec-volume"></div>', unsafe_allow_html=True)
        render_signal_volume(signals)
    if _has_perm("competitive_pulse"):
        st.markdown('<div id="lh-sec-competitive"></div>', unsafe_allow_html=True)
        render_competitive_pulse(signals, competitors_raw)
else:
    st.info("No dispatch saved yet. Switch to **Live mode** in the sidebar and press **⚡ Sweep & Generate** to create the first briefing.")

tab_dispatch.__exit__(None, None, None)

tab_projects.__enter__()

# ══════════════════════════════════════════════════════════════════════════════
# CURADORIA — Seleção e board coletivo
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
.cur-header {
    border-top: 3px double #071828; padding-top: 1.5rem; margin-top: 0.5rem;
}
.cur-title {
    font-family: Georgia, serif; font-size: 32px; font-weight: 600;
    color: #071828; margin: 6px 0 4px;
}
.cur-sub {
    font-family: Georgia, serif; font-style: italic;
    font-size: 15px; color: #274d68; margin: 0 0 1.5rem;
}
.cur-label {
    font-family: monospace; font-size: 10px; letter-spacing: .16em;
    text-transform: uppercase; color: #0a7d8c; font-weight: 700;
}
.cur-item {
    background: #fff !important; border: 1px solid #9dc4d8;
    border-left: 3px solid #0a7d8c; border-radius: 6px;
    padding: 14px 16px; margin-bottom: 10px;
}
.cur-item-type {
    font-family: monospace; font-size: 9px; letter-spacing: .12em;
    text-transform: uppercase; color: #0a7d8c !important; margin-bottom: 5px;
}
.cur-item-title {
    font-family: Georgia, serif; font-size: 15px;
    font-weight: 600; color: #071828 !important; margin-bottom: 5px; line-height: 1.3;
}
.cur-item-content {
    font-size: 13px; color: #274d68 !important; line-height: 1.5;
}
.cur-item-meta {
    font-family: monospace; font-size: 9px; color: #6ea8c4 !important;
    text-transform: uppercase; margin-top: 8px;
}
.cur-user-pill {
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-family: monospace; font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .06em; color: #fff !important; margin-right: 6px;
}
.cur-folder-pill {
    display: inline-block; padding: 2px 8px; border-radius: 3px;
    font-family: monospace; font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .06em; color: #fff !important;
    background: #1f4e80 !important; margin-right: 6px;
}
/* Thought Partner (Socratic) panel */
.tp {
    background: #eef5fc !important; border: 1px solid #cfe0f2; border-radius: 8px;
    padding: 22px 26px; margin-top: 1rem;
}
.tph {
    font-family: monospace; font-size: 9px; letter-spacing: .16em;
    text-transform: uppercase; color: #1f4e80 !important; font-weight: 700; margin-bottom: 6px;
}
.tpsub {
    font-family: Georgia, serif; font-style: italic; font-size: 14px;
    color: #274d68 !important; margin-bottom: 14px;
}
.tpgroup-title {
    font-family: monospace; font-size: 9px; text-transform: uppercase;
    letter-spacing: .12em; color: #1f4e80 !important; font-weight: 700;
    margin: 16px 0 8px;
}
.sugg {
    background: #fff !important; border: 1px solid #cfe0f2; border-left: 3px solid #1f4e80;
    border-radius: 6px; padding: 12px 16px; margin-bottom: 8px;
}
.sugg-text {
    font-family: Georgia, serif; font-size: 14px; color: #071828 !important; line-height: 1.55;
}
.tag {
    display: inline-block; padding: 2px 9px; border-radius: 10px;
    font-family: monospace; font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .08em;
    background: #1f4e80 !important; color: #fff !important; margin-right: 8px;
}
.caveat {
    font-family: monospace; font-size: 9px; letter-spacing: .1em; text-transform: uppercase;
    color: #6ea8c4 !important; margin-top: 14px;
}
</style>
""", unsafe_allow_html=True)

# Header
st.markdown("""
<div class="cur-header">
  <span class="cur-label">◈ Curation</span>
  <div class="cur-title">Insights Board</div>
  <div class="cur-sub">Select the most relevant content from the dispatch. Your board and your team's board live here.</div>
</div>
""", unsafe_allow_html=True)

# ── Project Folders bar ───────────────────────────────────────────────────────
# Lets the team organize saved insights into per-client/per-project folders.
# An item can live in several folders, or none ("Unsorted"). The active folder
# filters both the My Board and Team Board tabs below.
project_folders   = load_project_folders()
_all_board_items  = load_curadoria()

if "active_folder" not in st.session_state:
    st.session_state["active_folder"] = "all"

_folder_options = ["all", "unsorted"] + [f["id"] for f in project_folders]
if st.session_state["active_folder"] not in _folder_options:
    st.session_state["active_folder"] = "all"

_folder_labels = {
    "all":      f"All ({len(_all_board_items)})",
    "unsorted": f"Unsorted ({sum(1 for i in _all_board_items if not i.get('folder_ids'))})",
}
for _f in project_folders:
    _cnt = sum(1 for i in _all_board_items if _f["id"] in (i.get("folder_ids") or []))
    _folder_labels[_f["id"]] = f"📁 {_f['name']} ({_cnt})"

col_folder_filter, col_folder_new = st.columns([5, 2])
with col_folder_filter:
    st.session_state["active_folder"] = st.radio(
        "Project folder",
        options=_folder_options,
        format_func=lambda k: _folder_labels.get(k, k),
        horizontal=True,
        key="folder_filter_radio",
        index=_folder_options.index(st.session_state["active_folder"]),
        label_visibility="collapsed",
    )
with col_folder_new:
    with st.popover("📁 Project folders", use_container_width=True):
        with st.form("new_folder_form", clear_on_submit=True):
            new_folder_name = st.text_input("New folder", placeholder="e.g. Heinz Q3 Campaign", label_visibility="collapsed")
            if st.form_submit_button("+ Create folder", use_container_width=True):
                if create_project_folder(new_folder_name, st.session_state.logged_in_user):
                    st.rerun()
                else:
                    st.warning("Enter a unique, non-empty folder name.")
        if project_folders:
            st.markdown("---")
            st.caption("Manage folders")
            for _f in project_folders:
                fcol1, fcol2 = st.columns([4, 1])
                with fcol1:
                    st.caption(f"📁 {_f['name']}")
                with fcol2:
                    if st.button("🗑", key=f"delfolder_{_f['id']}", help=f"Delete '{_f['name']}'"):
                        delete_project_folder(_f["id"])
                        if st.session_state.get("active_folder") == _f["id"]:
                            st.session_state["active_folder"] = "all"
                        st.rerun()

# ── Client Access — admin panel ─────────────────────────────────────────────────
# Internal-only (clients st.stop() before reaching this point). Lets the team
# create read-only client logins and switch on individual analytics sections
# for each client (Topic Map, Momentum, Signal Volume, Competitive Pulse).
with st.popover("🔑 Client access", use_container_width=False):
    st.caption("Create read-only logins for clients and choose which sections they can see.")
    with st.form("new_client_form", clear_on_submit=True):
        new_client_label = st.text_input("Client name", placeholder="e.g. Heinz Soup UK")
        col_cu, col_cp = st.columns(2)
        with col_cu:
            new_client_user = st.text_input("Username")
        with col_cp:
            new_client_pass = st.text_input("Password", type="password")
        if st.form_submit_button("+ Create client login", use_container_width=True):
            if create_client_account(new_client_user, new_client_pass, new_client_label):
                st.rerun()
            else:
                st.warning("Enter a unique username and a non-empty password.")

    _client_accounts = load_client_accounts()
    if _client_accounts:
        st.markdown("---")
        st.caption("Manage access")
        for _acct in _client_accounts:
            st.markdown(f"**{e(_acct.get('client_label', _acct['username']))}** · `{e(_acct['username'])}`")
            _perms = dict(CLIENT_PERM_DEFAULTS, **_acct.get("perms", {}))
            _new_perms = {}
            for _pkey, _plabel in CLIENT_PERM_DEFS:
                _new_perms[_pkey] = st.checkbox(
                    _plabel, value=_perms.get(_pkey, False),
                    key=f"perm_{_acct['username']}_{_pkey}",
                )
            pcol1, pcol2 = st.columns([3, 1])
            with pcol1:
                if st.button("Save permissions", key=f"saveperm_{_acct['username']}", use_container_width=True):
                    update_client_perms(_acct["username"], _new_perms)
                    st.toast(f"✓ Permissions updated for {_acct.get('client_label', _acct['username'])}")
            with pcol2:
                if st.button("🗑", key=f"delclient_{_acct['username']}", help="Delete this client login"):
                    delete_client_account(_acct["username"])
                    st.rerun()
            st.markdown("---")
    else:
        st.caption("No client logins yet — create one above.")

# ── Database / Persistence panel (admin only) ─────────────────────────────────
with st.popover("🗄 Database", use_container_width=False):
    if _db.use_supabase():
        st.success("✅ Supabase connected — data persists across deploys.")
    else:
        st.warning("⚠️ Supabase not configured — using local files (data lost on redeploy).")
        st.caption("Add SUPABASE_URL + SUPABASE_KEY to `.streamlit/secrets.toml` to enable persistence.")

    st.markdown("---")
    st.caption("**Migrate existing file data → Supabase**")
    st.caption("Run this once to push your current JSON/JSONL files into Supabase. Safe to run again (uses upsert).")
    if st.button("⬆ Migrate files to Supabase", use_container_width=True, key="migrate_to_sb"):
        if not _db.use_supabase():
            st.error("Supabase not configured.")
        else:
            with st.spinner("Migrating…"):
                try:
                    counts = _db.migrate_files_to_supabase()
                    st.success("Migration complete: " + " · ".join(f"{v} {k}" for k, v in counts.items()))
                except Exception as _exc:
                    st.error(f"Migration failed: {_exc}")

    if _db.use_supabase():
        st.markdown("---")
        st.caption("**Sweep history**")
        _sweep_runs = _db.load_sweep_runs(limit=10)
        if _sweep_runs:
            for _run in _sweep_runs[:5]:
                _run_at = str(_run.get("run_at", ""))[:16].replace("T", " ")
                st.markdown(
                    f"`{_run_at}` · **{_run.get('topic','?')}** · {_run.get('signal_count',0)} signals"
                )
        else:
            st.caption("No sweep runs recorded yet.")

# ── Wireframe 2-col project view ──────────────────────────────────────────────
# Follows the wireframe spec exactly: project selector → left col (collected
# currents + Add a current with URL fetch) · right col (Thought Partner).
# My Board, Team Board and Briefing Builder move to expanders below.

st.markdown("""<style>
.mini-current {
    background: #fff !important;
    border: 1px solid #e4e2db;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 8px;
}
.mini-current-title {
    font-size: 13.5px;
    font-weight: 500;
    color: #071828 !important;
    margin-bottom: 5px;
    line-height: 1.35;
}
.chip-meta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: #6ea8c4 !important;
    text-transform: uppercase;
    letter-spacing: .06em;
}
.dashed-add {
    border: 1px dashed #9dc4d8;
    border-radius: 8px;
    color: #6ea8c4 !important;
    text-align: center;
    font-size: 13px;
    padding: 12px;
    margin-top: 4px;
    cursor: pointer;
}
.pill-internal {
    display: inline-block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: .08em;
    text-transform: uppercase;
    background: #f7eddf;
    color: #7c4912 !important;
    border-radius: 8px;
    padding: 3px 10px;
    margin-left: 10px;
    vertical-align: middle;
}
</style>""", unsafe_allow_html=True)

# ── Helper: human-readable source chip ────────────────────────────────────────
def _source_chip(type_str: str) -> str:
    t = (type_str or "").lower()
    if "url" in t:        return "manual · url"
    if "search" in t:     return "from search"
    if "gdelt" in t:      return "from search · gdelt"
    if "exa" in t:        return "from search · exa"
    if "tavily" in t:     return "from search · tavily"
    if "card" in t:       return "from dispatch"
    if "voice" in t:      return "from dispatch"
    if "prov" in t:       return "from dispatch"
    return "saved"

# ── MAIN 2-COL PROJECT VIEW ───────────────────────────────────────────────────
_proj_user = st.session_state.logged_in_user

if not project_folders:
    st.markdown("""
<div style="border:1px dashed #9dc4d8;border-radius:10px;padding:32px;text-align:center;margin:24px 0;">
  <div style="font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#6ea8c4;margin-bottom:8px;">No projects yet</div>
  <div style="font-family:Georgia,serif;font-size:16px;color:#274d68;">Create a project folder above, then use <strong>"+ Add to project"</strong> on dispatch cards or search results to start collecting currents here.</div>
</div>""", unsafe_allow_html=True)
else:
    # ── Project selector ──────────────────────────────────────────────────────
    _pf_col, _pf_new = st.columns([7, 3])
    with _pf_col:
        _sel_proj_id = st.selectbox(
            "Project",
            options=[f["id"] for f in project_folders],
            format_func=lambda fid: next((f["name"] for f in project_folders if f["id"] == fid), fid),
            key="proj_main_select",
            label_visibility="collapsed",
        )
    with _pf_new:
        with st.popover("＋ New project", use_container_width=True):
            with st.form("new_folder_wf", clear_on_submit=True):
                _nfname = st.text_input("Project name", placeholder="e.g. Heinz Q3 Campaign", label_visibility="collapsed")
                if st.form_submit_button("Create", use_container_width=True):
                    if create_project_folder(_nfname, _proj_user):
                        st.rerun()
                    else:
                        st.warning("Enter a unique, non-empty name.")

    _sel_proj_name = next((f["name"] for f in project_folders if f["id"] == _sel_proj_id), _sel_proj_id)
    _proj_items    = [i for i in _all_board_items if _sel_proj_id in (i.get("folder_ids") or [])]

    # Project header
    st.markdown(f"""
<div style="margin:18px 0 20px;">
  <div style="font-family:Georgia,serif;font-size:26px;font-weight:600;color:#071828;display:inline;">
    {e(_sel_proj_name)}</div>
  <span class="pill-internal">Internal only</span>
  <div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:#9dc4d8;margin-top:4px;letter-spacing:.06em;text-transform:uppercase;">
    {len(_proj_items)} current{'s' if len(_proj_items) != 1 else ''} collected</div>
</div>""", unsafe_allow_html=True)

    # ── 2-col wireframe layout ────────────────────────────────────────────────
    col_currents, col_tp = st.columns([6, 4], gap="large")

    # ── LEFT: Collected currents ──────────────────────────────────────────────
    with col_currents:
        st.markdown('<p class="chip-meta" style="margin-bottom:10px;">Collected currents</p>', unsafe_allow_html=True)

        if not _proj_items:
            st.markdown("""
<div style="border:1px solid #e4e2db;border-radius:8px;padding:18px;color:#9b9e97;font-size:13px;text-align:center;margin-bottom:10px;">
  No currents collected yet — use "＋ Add to project" on dispatch cards or search results.
</div>""", unsafe_allow_html=True)
        else:
            for _pit in reversed(_proj_items):
                _pit_date = (_pit.get("saved_at") or "")[:6]  # "09 Jun"
                _pit_src  = _source_chip(_pit.get("type", ""))
                _pit_cat  = _pit.get("category") or ""
                _pit_meta = " · ".join(filter(None, [_pit_src, _pit_date, _pit_cat]))
                _mc_col, _mc_del = st.columns([11, 1])
                with _mc_col:
                    _pit_url = _pit.get("url", "")
                    _pit_link = (
                        f' <a href="{e(_pit_url)}" target="_blank" '
                        f'style="font-family:JetBrains Mono,monospace;font-size:9px;color:#0a7d8c;'
                        f'text-decoration:none;">↗</a>'
                    ) if _pit_url else ""
                    st.markdown(f"""
<div class="mini-current">
  <div class="mini-current-title">{e(_pit.get('title','')[:90])}{_pit_link}</div>
  <span class="chip-meta">{e(_pit_meta)}</span>
</div>""", unsafe_allow_html=True)
                with _mc_del:
                    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)
                    if st.button("×", key=f"proj_rem_{_pit['id']}_{_sel_proj_id}",
                                 help="Remove from this project"):
                        _new_fids = [f for f in _pit.get("folder_ids", []) if f != _sel_proj_id]
                        set_item_folders(_pit["id"], _new_fids)
                        st.rerun()

        # ── Dashed "+ Add a current" ──────────────────────────────────────────
        with st.expander("＋ Add a current", expanded=False):
            _add_url  = st.text_input("URL", key="add_curr_url", placeholder="https://…",
                                      label_visibility="collapsed")
            _add_note = st.text_input("Note (optional)", key="add_curr_note",
                                      placeholder="Why is this relevant?", label_visibility="collapsed")
            if st.button("Fetch & save as current", key="btn_add_curr", use_container_width=True,
                         disabled=not _add_url):
                with st.spinner("Fetching the URL and extracting the insight via Claude…"):
                    try:
                        _new_curr = add_url_current(
                            _add_url, _proj_user, _sel_proj_id, _add_note
                        )
                        st.success(f"✓ Saved: {_new_curr.get('title','')[:60]}")
                        st.rerun()
                    except Exception as _url_err:
                        st.error(f"Could not fetch the URL: {_url_err}")

    # ── RIGHT: Thought Partner ────────────────────────────────────────────────
    with col_tp:
        if not _proj_items:
            st.markdown("""
<div class="tp" style="opacity:.55;text-align:center;padding:28px;">
  <div class="tph">🧭 Thought partner</div>
  <div class="tpsub">Add at least one current to activate the thought partner.</div>
</div>""", unsafe_allow_html=True)
        else:
            if st.button("🧭 Explore tensions & angles",
                         key=f"projtp_wf_{_sel_proj_id}", use_container_width=True):
                _tp_api = os.environ.get("ANTHROPIC_API_KEY")
                if not _tp_api:
                    st.error("ANTHROPIC_API_KEY not set.")
                else:
                    try:
                        import anthropic as _ant_tp
                        _ant_tp_client = _ant_tp.Anthropic(api_key=_tp_api)
                        _tp_insights = "\n\n".join(
                            f"[{it['type']}] {it['title']}\n{it.get('content','')}"
                            for it in _proj_items
                        )
                        _tp_prompt = f"""You are a thought partner for a creative strategy team — not a strategist
delivering a final answer. Your job is to open up thinking, not close it down.

Project: {_sel_proj_name}

Collected currents for this project (and only these):

{_tp_insights}

Based on these signals, surface possible tensions and angles worth discussing — framed as
open questions and observations, never as conclusions or recommendations. Avoid words like
"should", "the strategy is", or "the answer is". Stay genuinely exploratory. Reason ONLY over
the currents collected for this project — do not invent outside context.

Return ONLY valid JSON with this exact structure:

{{
  "framing": "1-2 sentences setting up what's interesting or unresolved here — a question, not a thesis.",
  "tensions": [
    {{"label": "short 2-4 word tag", "text": "1-2 sentences describing a tension or contradiction in the signals, posed as something to weigh, not resolve."}},
    {{"label": "short 2-4 word tag", "text": "..."}},
    {{"label": "short 2-4 word tag", "text": "..."}}
  ],
  "angles": [
    {{"label": "short 2-4 word tag", "text": "1-2 sentences describing a possible creative angle or direction — framed as 'what if' or 'one way in could be', not a final recommendation."}},
    {{"label": "short 2-4 word tag", "text": "..."}},
    {{"label": "short 2-4 word tag", "text": "..."}}
  ],
  "questions_for_team": [
    "An open question for the team to debate in the next meeting.",
    "Another open question.",
    "A third open question."
  ]
}}"""
                        with st.spinner("The Lighthouse is mapping tensions and angles…"):
                            _tp_msg = _ant_tp_client.messages.create(
                                model=CLAUDE_MODEL,
                                max_tokens=2048,
                                temperature=0.85,
                                system="You are a Socratic thought partner. Return only raw JSON, no markdown fences. Never give final recommendations — only tensions, angles and questions.",
                                messages=[{"role": "user", "content": _tp_prompt}],
                            )
                            _tp_raw = _tp_msg.content[0].text.strip()
                            if "project_thought_partner" not in st.session_state:
                                st.session_state["project_thought_partner"] = {}
                            st.session_state["project_thought_partner"][_sel_proj_id] = _extract_json(_tp_raw)
                    except json.JSONDecodeError as _tp_ex:
                        st.error(f"Exploration failed: the response wasn't valid JSON ({_tp_ex}).")
                        with st.expander("Show raw response"):
                            st.code(_tp_raw)
                    except Exception as _tp_ex:
                        st.error(f"Exploration failed: {_tp_ex}")

            _tp_result = st.session_state.get("project_thought_partner", {}).get(_sel_proj_id)
            if _tp_result:
                _tensions_html = "".join(
                    f'<div class="sugg"><span class="tag">{e(t.get("label",""))}</span>'
                    f'<span class="sugg-text"> {e(t.get("text",""))}</span></div>'
                    for t in _tp_result.get("tensions", [])
                )
                _angles_html = "".join(
                    f'<div class="sugg"><span class="tag">{e(a.get("label",""))}</span>'
                    f'<span class="sugg-text"> {e(a.get("text",""))}</span></div>'
                    for a in _tp_result.get("angles", [])
                )
                _questions_html = "".join(
                    f'<div class="sugg"><span class="sugg-text">? {e(q)}</span></div>'
                    for q in _tp_result.get("questions_for_team", [])
                )
                st.markdown(f"""
<div class="tp">
  <div class="tph">🧭 Thought partner · {e(_sel_proj_name)}</div>
  <div class="tpsub">{e(_tp_result.get("framing",""))}</div>
  <div class="tpgroup-title">Possible tensions</div>
  {_tensions_html}
  <div class="tpgroup-title">Angles to explore</div>
  {_angles_html}
  <div class="tpgroup-title">Questions for the team</div>
  {_questions_html}
  <div class="caveat">Suggestions, not conclusions — for the team to debate.</div>
</div>""", unsafe_allow_html=True)
                # ── Ask about this project ────────────────────────────────────
                _ask_q = st.text_input(
                    "Ask about this project…",
                    key=f"proj_ask_{_sel_proj_id}",
                    placeholder="e.g. What's the strongest angle here?",
                    label_visibility="collapsed",
                )
                if _ask_q and st.button("Ask", key=f"proj_ask_btn_{_sel_proj_id}"):
                    _ask_api = os.environ.get("ANTHROPIC_API_KEY")
                    if _ask_api:
                        import anthropic as _ant_ask
                        _ask_client = _ant_ask.Anthropic(api_key=_ask_api)
                        _ask_ctx = "\n".join(
                            f"[{it['type']}] {it['title']}: {it.get('content','')[:200]}"
                            for it in _proj_items
                        )
                        with st.spinner("Thinking…"):
                            _ask_msg = _ask_client.messages.create(
                                model=CLAUDE_MODEL,
                                max_tokens=512,
                                temperature=0.7,
                                system="You are a Socratic thought partner. Answer the question by posing more open questions and surfacing tensions — never close down the thinking or give definitive recommendations.",
                                messages=[{"role": "user", "content": f"Project: {_sel_proj_name}\n\nCollected currents:\n{_ask_ctx}\n\nQuestion from team: {_ask_q}"}],
                            )
                            st.markdown(f"""
<div class="sugg" style="margin-top:12px;">
  <span class="tag">response</span>
  <span class="sugg-text"> {e(_ask_msg.content[0].text)}</span>
</div>""", unsafe_allow_html=True)
            else:
                st.caption('Click "Explore tensions & angles" to get started.')

# ── Team Board (expander) ─────────────────────────────────────────────────────
st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
with st.expander("📋 Team Board · All saved insights", expanded=False):
    _tb_tabs = st.tabs([
        f"  My Board ({st.session_state.logged_in_user})  ",
        "  Team Board  ",
    ])
    with _tb_tabs[0]:
        current_user = st.session_state.logged_in_user
        my_items_all = [i for i in load_curadoria() if i["user"] == current_user]
        my_items     = _filter_items_by_active_folder(my_items_all)

    if not my_items_all:
        st.info("Your board is empty. Use the 🔖 buttons throughout the dispatch to save insights.")
    elif not my_items:
        st.info("No items in this folder yet. Use the 📁 button on a saved item to add it here.")
    else:
        st.markdown(f"**{len(my_items)} item{'s' if len(my_items) != 1 else ''} saved**")
        for item in reversed(my_items):
            _render_board_item(
                item, project_folders,
                color=USER_COLORS.get(current_user, "#0a7d8c"),
                show_user_pill=False, allow_delete=True, ctx="my",
            )


# ── Team Board tab (inside expander) ─────────────────────────────────────────
with _tb_tabs[1]:
    all_items_all = load_curadoria()
    all_items     = _filter_items_by_active_folder(all_items_all)

    if not all_items_all:
        st.info("No items saved yet. Use the 🔖 buttons throughout the dispatch to save insights.")
    elif not all_items:
        st.info("No items in this folder yet. Use the 📁 button on a saved item to add it here.")
    else:
        # Group by user
        by_user = {}
        for item in all_items:
            by_user.setdefault(item["user"], []).append(item)

        total = len(all_items)
        st.markdown(f"**{total} insight{'s' if total != 1 else ''} saved by the team** · {len(by_user)} member{'s' if len(by_user) != 1 else ''}")
        st.markdown("---")

        current_user = st.session_state.logged_in_user
        for user_name, items in by_user.items():
            color = USER_COLORS.get(user_name, "#0a7d8c")
            st.markdown(f"""
<div style="display:flex;align-items:center;gap:10px;margin:1.2rem 0 0.8rem">
  <div style="width:32px;height:32px;border-radius:50%;background:{color};display:flex;align-items:center;justify-content:center;font-family:Georgia,serif;font-weight:600;font-size:14px;color:#fff;flex:none">{user_name[0]}</div>
  <span style="font-family:Georgia,serif;font-size:18px;font-weight:600;color:#071828">{e(user_name)}</span>
  <span style="font-family:monospace;font-size:10px;color:#9dc4d8;text-transform:uppercase;letter-spacing:.1em">{len(items)} item{'ns' if len(items) != 1 else ''}</span>
</div>""", unsafe_allow_html=True)

            for item in reversed(items):
                _render_board_item(
                    item, project_folders, color=color,
                    show_user_pill=True, allow_delete=(user_name == current_user), ctx="team",
                )


# ── Briefing Builder (expander) ───────────────────────────────────────────────
with st.expander("✍ Briefing Builder · Turn saved insights into a creative brief", expanded=False):
    current_user = st.session_state.logged_in_user
    my_items     = [i for i in load_curadoria() if i["user"] == current_user]

    st.markdown("""
<div style="border-top:2px solid #071828;padding-top:1.2rem;margin-bottom:1rem;">
  <div style="font-family:monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;
       color:#0a7d8c;font-weight:700;margin-bottom:4px;">✍ Briefing Builder</div>
  <div style="font-family:Georgia,serif;font-size:22px;font-weight:600;color:#071828;margin-bottom:6px;">
    Turn your saved insights into a creative brief</div>
  <div style="font-family:Georgia,serif;font-style:italic;font-size:14px;color:#274d68;">
    Select items on My Board, then generate a structured brief ready to share with the creative team.</div>
</div>""", unsafe_allow_html=True)

    if not my_items:
        st.info("Your board is empty. Save insights from the dispatch using the 🔖 buttons, then come back here.")
    else:
        # Show items as checkboxes
        st.markdown(f"**{len(my_items)} insight{'s' if len(my_items)!=1 else ''} on your board** — select which to include:")
        selected_items = []
        for item in reversed(my_items):
            label = f"**{item['type']}** · {item['title'][:60]}"
            if st.checkbox(label, value=True, key=f"brief_sel_{item['id']}"):
                selected_items.append(item)

        st.markdown("---")

        brief_client   = st.text_input("Client", value=client_name,  key="brief_client")
        brief_tagline  = st.text_input("Brief context", value=brief_tagline, key="brief_ctx")

        st.markdown("<div style='margin-top:4px'></div>", unsafe_allow_html=True)
        bcol_full, bcol_socratic = st.columns(2)
        with bcol_full:
            gen_full_brief = st.button("⚡ Generate Creative Brief", use_container_width=True, disabled=not selected_items)
        with bcol_socratic:
            gen_thought_partner = st.button(
                "🧭 Explore Tensions & Angles", use_container_width=True, disabled=not selected_items,
                help="A more Socratic mode — surfaces open questions and angles to debate, not a finished brief.",
            )

        if gen_full_brief:
            if not selected_items:
                st.warning("Select at least one insight to build a brief.")
            else:
                api_key = os.environ.get("ANTHROPIC_API_KEY")
                if not api_key:
                    st.error("ANTHROPIC_API_KEY not found.")
                else:
                    try:
                        import anthropic as _ant
                        _client = _ant.Anthropic(api_key=api_key)

                        insights_text = "\n\n".join(
                            f"[{it['type']}] {it['title']}\n{it['content']}"
                            for it in selected_items
                        )

                        brief_prompt = f"""You are a senior strategist at a world-class advertising agency.

Client: {brief_client}
Context: {brief_tagline}

Selected cultural insights from The Lighthouse dispatch:

{insights_text}

Write a tight, actionable creative brief. Return ONLY valid JSON with this exact structure:

{{
  "audience_insight": "2-3 sentences. Who they are, what they're feeling right now, the specific tension.",
  "cultural_tension": "1-2 sentences. The fault line in culture this campaign can own.",
  "strategic_direction": "1 sentence. The single-minded thought. Bold and specific.",
  "execution_ideas": ["Idea 1 — specific format + platform", "Idea 2", "Idea 3"],
  "timing_window": "When to move and why. Reference the signals.",
  "proof_point": "The key signal or quote that justifies this direction."
}}"""

                        with st.spinner("The Lighthouse is writing your brief…"):
                            msg = _client.messages.create(
                                model=CLAUDE_MODEL,
                                max_tokens=2048,
                                temperature=0.7,
                                system="You are an elite advertising strategist. Return only raw JSON, no markdown fences.",
                                messages=[{"role": "user", "content": brief_prompt}],
                            )
                            raw = msg.content[0].text.strip()
                            brief_data = _extract_json(raw)
                            st.session_state["generated_brief"] = brief_data

                    except json.JSONDecodeError as ex:
                        st.error(f"Brief generation failed: the response wasn't valid JSON ({ex}).")
                        with st.expander("Show raw response"):
                            st.code(raw)
                    except Exception as ex:
                        st.error(f"Brief generation failed: {ex}")

        # Display generated brief
        if "generated_brief" in st.session_state:
            bd = st.session_state["generated_brief"]
            st.markdown(f"""
<div style="background:#fff;border:1px solid #9dc4d8;border-radius:8px;padding:28px 32px;margin-top:1rem;">
  <div style="font-family:monospace;font-size:9px;letter-spacing:.16em;text-transform:uppercase;
       color:#0a7d8c;font-weight:700;margin-bottom:14px;">Creative Brief · {brief_client}</div>

  <div style="font-family:monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;
       color:#274d68;margin-bottom:4px;">Audience Insight</div>
  <div style="font-family:Georgia,serif;font-size:15px;color:#071828;line-height:1.55;margin-bottom:16px;">
    {e(bd.get('audience_insight',''))}</div>

  <div style="font-family:monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;
       color:#274d68;margin-bottom:4px;">Cultural Tension</div>
  <div style="font-family:Georgia,serif;font-size:15px;color:#071828;line-height:1.55;margin-bottom:16px;">
    {e(bd.get('cultural_tension',''))}</div>

  <div style="font-family:monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;
       color:#274d68;margin-bottom:4px;">Strategic Direction</div>
  <div style="font-family:Georgia,serif;font-size:18px;font-weight:600;color:#071828;
       line-height:1.3;margin-bottom:16px;border-left:3px solid #0a7d8c;padding-left:14px;">
    {e(bd.get('strategic_direction',''))}</div>

  <div style="font-family:monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;
       color:#274d68;margin-bottom:6px;">Execution Ideas</div>
  {''.join(f'<div style="font-family:Georgia,serif;font-size:14px;color:#071828;padding:6px 0 6px 14px;border-left:2px solid #9dc4d8;margin-bottom:6px;">→ {e(idea)}</div>' for idea in bd.get('execution_ideas',[]))}

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px;">
    <div>
      <div style="font-family:monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:#274d68;margin-bottom:4px;">Timing Window</div>
      <div style="font-family:Georgia,serif;font-size:14px;color:#071828;line-height:1.5;">{e(bd.get('timing_window',''))}</div>
    </div>
    <div>
      <div style="font-family:monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:#274d68;margin-bottom:4px;">Proof Point</div>
      <div style="font-family:Georgia,serif;font-style:italic;font-size:14px;color:#274d68;line-height:1.5;">"{e(bd.get('proof_point',''))}"</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

            # Download brief as text
            brief_txt = f"""CREATIVE BRIEF — {brief_client}
{"="*60}

AUDIENCE INSIGHT
{bd.get('audience_insight','')}

CULTURAL TENSION
{bd.get('cultural_tension','')}

STRATEGIC DIRECTION
{bd.get('strategic_direction','')}

EXECUTION IDEAS
{chr(10).join(f"→ {i}" for i in bd.get('execution_ideas',[]))}

TIMING WINDOW
{bd.get('timing_window','')}

PROOF POINT
"{bd.get('proof_point','')}\"

Generated by The Lighthouse · Atlantic Intelligence Layer
"""
            st.download_button(
                "↓ Download Brief",
                data=brief_txt,
                file_name=f"brief_{datetime.utcnow().strftime('%Y%m%d')}.txt",
                mime="text/plain",
                use_container_width=True,
            )

        # ── Socratic / Thought Partner mode ──────────────────────────────────
        # Unlike the brief above, this mode never concludes — it surfaces
        # tensions, angles and open questions for the team to debate.
        if gen_thought_partner:
            if not selected_items:
                st.warning("Select at least one insight to explore.")
            else:
                api_key = os.environ.get("ANTHROPIC_API_KEY")
                if not api_key:
                    st.error("ANTHROPIC_API_KEY not found.")
                else:
                    try:
                        import anthropic as _ant
                        _client = _ant.Anthropic(api_key=api_key)

                        insights_text = "\n\n".join(
                            f"[{it['type']}] {it['title']}\n{it['content']}"
                            for it in selected_items
                        )

                        tp_prompt = f"""You are a thought partner for a creative strategy team — not a strategist
delivering a final answer. Your job is to open up thinking, not close it down.

Client: {brief_client}
Context: {brief_tagline}

Selected cultural insights from The Lighthouse dispatch:

{insights_text}

Based on these signals, surface possible tensions and angles worth discussing — framed as
open questions and observations, never as conclusions or recommendations. Avoid words like
"should", "the strategy is", or "the answer is". Stay genuinely exploratory.

Return ONLY valid JSON with this exact structure:

{{
  "framing": "1-2 sentences setting up what's interesting or unresolved here — a question, not a thesis.",
  "tensions": [
    {{"label": "short 2-4 word tag", "text": "1-2 sentences describing a tension or contradiction in the signals, posed as something to weigh, not resolve."}},
    {{"label": "short 2-4 word tag", "text": "..."}},
    {{"label": "short 2-4 word tag", "text": "..."}}
  ],
  "angles": [
    {{"label": "short 2-4 word tag", "text": "1-2 sentences describing a possible creative angle or direction — framed as 'what if' or 'one way in could be', not a final recommendation."}},
    {{"label": "short 2-4 word tag", "text": "..."}},
    {{"label": "short 2-4 word tag", "text": "..."}}
  ],
  "questions_for_team": [
    "An open question for the team to debate in the next meeting.",
    "Another open question.",
    "A third open question."
  ]
}}"""

                        with st.spinner("The Lighthouse is mapping tensions and angles…"):
                            msg = _client.messages.create(
                                model=CLAUDE_MODEL,
                                max_tokens=2048,
                                temperature=0.85,
                                system="You are a Socratic thought partner. Return only raw JSON, no markdown fences. Never give final recommendations — only tensions, angles and questions.",
                                messages=[{"role": "user", "content": tp_prompt}],
                            )
                            raw = msg.content[0].text.strip()
                            st.session_state["thought_partner"] = _extract_json(raw)

                    except json.JSONDecodeError as ex:
                        st.error(f"Exploration failed: the response wasn't valid JSON ({ex}).")
                        with st.expander("Show raw response"):
                            st.code(raw)
                    except Exception as ex:
                        st.error(f"Exploration failed: {ex}")

        # Display thought-partner panel
        if "thought_partner" in st.session_state:
            tp = st.session_state["thought_partner"]
            tensions_html = "".join(
                f'<div class="sugg"><span class="tag">{e(t.get("label",""))}</span>'
                f'<span class="sugg-text">{e(t.get("text",""))}</span></div>'
                for t in tp.get("tensions", [])
            )
            angles_html = "".join(
                f'<div class="sugg"><span class="tag">{e(a.get("label",""))}</span>'
                f'<span class="sugg-text">{e(a.get("text",""))}</span></div>'
                for a in tp.get("angles", [])
            )
            questions_html = "".join(
                f'<div class="sugg"><span class="sugg-text">? {e(q)}</span></div>'
                for q in tp.get("questions_for_team", [])
            )
            st.markdown(f"""
<div class="tp">
  <div class="tph">🧭 Thought Partner · {e(brief_client)}</div>
  <div class="tpsub">{e(tp.get("framing",""))}</div>

  <div class="tpgroup-title">Possible Tensions</div>
  {tensions_html}

  <div class="tpgroup-title">Angles to Explore</div>
  {angles_html}

  <div class="tpgroup-title">Questions for the Team</div>
  {questions_html}

  <div class="caveat">Suggestions, not conclusions — for the team to debate.</div>
</div>""", unsafe_allow_html=True)

# ── OLD Project Thought Partner tab — now handled inline above. ───────────────
# This block is intentionally replaced by the 2-col wireframe layout; keeping
# the dead `with` alive would raise NameError. Wrap in `if False:` to silence
# static-analysis warnings while we clean up.
if False:
    _unused_tp_block = True  # placeholder — real logic is in the 2-col section
if False:  # old block kept for reference, never executed
    st.markdown("""
<div style="border-top:2px solid #071828;padding-top:1.2rem;margin-bottom:1rem;">
  <div style="font-family:monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;
       color:#0a7d8c;font-weight:700;margin-bottom:4px;">🧭 Project Thought Partner</div>
  <div style="font-family:Georgia,serif;font-size:22px;font-weight:600;color:#071828;margin-bottom:6px;">
    Reason over one project's collected currents</div>
  <div style="font-family:Georgia,serif;font-style:italic;font-size:14px;color:#274d68;">
    Pick a project folder — the thought partner reasons only over what's been collected there. It suggests, never answers.</div>
</div>""", unsafe_allow_html=True)

    if not project_folders:
        st.info("No project folders yet. Create one above, then use \"+ Add to project\" on dispatch cards or search results to start a focused session here.")
    else:
        tp_folder_id = st.selectbox(
            "Project",
            options=[f["id"] for f in project_folders],
            format_func=lambda fid: next((f["name"] for f in project_folders if f["id"] == fid), fid),
            key="proj_tp_folder",
        )
        tp_folder_name = next((f["name"] for f in project_folders if f["id"] == tp_folder_id), tp_folder_id)
        tp_items = [i for i in _all_board_items if tp_folder_id in (i.get("folder_ids") or [])]

        col_items, col_tp = st.columns(2)

        with col_items:
            st.markdown(f"**{len(tp_items)} current{'s' if len(tp_items) != 1 else ''} collected in {e(tp_folder_name)}**")
            if not tp_items:
                st.info('Nothing collected yet. Use "+ Add to project" on dispatch cards or search results to send currents here.')
            else:
                for item in reversed(tp_items):
                    color = USER_COLORS.get(item["user"], "#0a7d8c")
                    _render_board_item(
                        item, project_folders, color=color,
                        show_user_pill=True, allow_delete=False, ctx="projtp",
                    )

        with col_tp:
            if not tp_items:
                st.caption("The thought partner needs at least one collected current to work with.")
            else:
                if st.button("🧭 Explore tensions & angles", key=f"projtp_btn_{tp_folder_id}", use_container_width=True):
                    api_key = os.environ.get("ANTHROPIC_API_KEY")
                    if not api_key:
                        st.error("ANTHROPIC_API_KEY not found.")
                    else:
                        try:
                            import anthropic as _ant
                            _client = _ant.Anthropic(api_key=api_key)

                            insights_text = "\n\n".join(
                                f"[{it['type']}] {it['title']}\n{it['content']}"
                                for it in tp_items
                            )

                            tp_prompt = f"""You are a thought partner for a creative strategy team — not a strategist
delivering a final answer. Your job is to open up thinking, not close it down.

Project: {tp_folder_name}

Collected currents for this project (and only these):

{insights_text}

Based on these signals, surface possible tensions and angles worth discussing — framed as
open questions and observations, never as conclusions or recommendations. Avoid words like
"should", "the strategy is", or "the answer is". Stay genuinely exploratory. Reason ONLY over
the currents collected for this project — do not invent outside context.

Return ONLY valid JSON with this exact structure:

{{
  "framing": "1-2 sentences setting up what's interesting or unresolved here — a question, not a thesis.",
  "tensions": [
    {{"label": "short 2-4 word tag", "text": "1-2 sentences describing a tension or contradiction in the signals, posed as something to weigh, not resolve."}},
    {{"label": "short 2-4 word tag", "text": "..."}},
    {{"label": "short 2-4 word tag", "text": "..."}}
  ],
  "angles": [
    {{"label": "short 2-4 word tag", "text": "1-2 sentences describing a possible creative angle or direction — framed as 'what if' or 'one way in could be', not a final recommendation."}},
    {{"label": "short 2-4 word tag", "text": "..."}},
    {{"label": "short 2-4 word tag", "text": "..."}}
  ],
  "questions_for_team": [
    "An open question for the team to debate in the next meeting.",
    "Another open question.",
    "A third open question."
  ]
}}"""

                            with st.spinner("The Lighthouse is mapping tensions and angles…"):
                                msg = _client.messages.create(
                                    model=CLAUDE_MODEL,
                                    max_tokens=2048,
                                    temperature=0.85,
                                    system="You are a Socratic thought partner. Return only raw JSON, no markdown fences. Never give final recommendations — only tensions, angles and questions.",
                                    messages=[{"role": "user", "content": tp_prompt}],
                                )
                                raw = msg.content[0].text.strip()
                                if "project_thought_partner" not in st.session_state:
                                    st.session_state["project_thought_partner"] = {}
                                st.session_state["project_thought_partner"][tp_folder_id] = _extract_json(raw)

                        except json.JSONDecodeError as ex:
                            st.error(f"Exploration failed: the response wasn't valid JSON ({ex}).")
                            with st.expander("Show raw response"):
                                st.code(raw)
                        except Exception as ex:
                            st.error(f"Exploration failed: {ex}")

                tp_result = st.session_state.get("project_thought_partner", {}).get(tp_folder_id)
                if tp_result:
                    tensions_html = "".join(
                        f'<div class="sugg"><span class="tag">{e(t.get("label",""))}</span>'
                        f'<span class="sugg-text">{e(t.get("text",""))}</span></div>'
                        for t in tp_result.get("tensions", [])
                    )
                    angles_html = "".join(
                        f'<div class="sugg"><span class="tag">{e(a.get("label",""))}</span>'
                        f'<span class="sugg-text">{e(a.get("text",""))}</span></div>'
                        for a in tp_result.get("angles", [])
                    )
                    questions_html = "".join(
                        f'<div class="sugg"><span class="sugg-text">? {e(q)}</span></div>'
                        for q in tp_result.get("questions_for_team", [])
                    )
                    st.markdown(f"""
<div class="tp">
  <div class="tph">🧭 Thought Partner · {e(tp_folder_name)}</div>
  <div class="tpsub">{e(tp_result.get("framing",""))}</div>

  <div class="tpgroup-title">Possible Tensions</div>
  {tensions_html}

  <div class="tpgroup-title">Angles to Explore</div>
  {angles_html}

  <div class="tpgroup-title">Questions for the Team</div>
  {questions_html}

  <div class="caveat">Suggestions, not conclusions — for the team to debate.</div>
</div>""", unsafe_allow_html=True)
                else:
                    st.caption('Click "Explore tensions & angles" to get started.')

tab_projects.__exit__(None, None, None)

# ── Trends tab: pre-create containers in display order ─────────────────────
# Research Lab first → Evidence second → Signal Lab last
# Streamlit containers can be filled out-of-order; the display order
# is determined by when the containers were created, not when filled.
tab_trends.__enter__()
_tr_ctr_research = st.container()   # 01 Research Lab  ← fills from line ~5022
_tr_ctr_evidence = st.container()   # 02 Evidence      ← fills from line ~4689
_tr_ctr_signals  = st.container()   # 03 Signal Lab    ← fills from line ~6476
tab_trends.__exit__(None, None, None)

_tr_ctr_evidence.__enter__()

# ══════════════════════════════════════════════════════════════════════════════
# EVIDENCE — signal gallery (folded into Trends tab)
# User types a hypothesis; Claude classifies signals as confirms/contradicts/
# complicates. Clean minimal UI. Sources: DB signals + live TikTok/IG/YouTube.
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
/* ── Evidence tab — clean minimalist shell ── */
.ev-header { padding: 2.5rem 0 1.5rem; text-align: center; }
.ev-eyebrow { font-family: monospace; font-size: 10px; letter-spacing: .18em;
  text-transform: uppercase; color: #6ea8c4; margin-bottom: 8px; }
.ev-title { font-family: Georgia, serif; font-size: 1.6rem; font-weight: 400;
  color: #071828; margin-bottom: 4px; }
.ev-sub { font-size: 13px; color: #6ea8c4; }

/* Evidence cards */
.ev-card { border: 0.5px solid #cde0ea; border-radius: 10px;
  background: #fff; padding: 16px 18px; margin-bottom: 10px; }
.ev-card-top { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
.ev-src { font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
  padding: 3px 9px; border-radius: 4px; font-weight: 600; }
.ev-src-reddit   { background:#fdefd5; color:#7a4a0a; }
.ev-src-rss      { background:#e4f4f5; color:#0a5560; }
.ev-src-gdelt    { background:#ede9fe; color:#4c3494; }
.ev-src-trends   { background:#fce7f3; color:#831843; }
.ev-src-hn       { background:#fff3e0; color:#7c4a00; }
.ev-src-youtube  { background:#fce8e8; color:#8b0000; }
.ev-src-tiktok   { background:#e8f5e9; color:#1b5e20; }
.ev-src-instagram{ background:#f3e5f5; color:#4a148c; }
.ev-src-twitter  { background:#e7f3ff; color:#003566; }
.ev-verdict { font-size: 10px; padding: 3px 9px; border-radius: 4px; letter-spacing: .06em; font-weight: 600; }
.ev-confirms    { background:#ecfdf5; color:#065f46; }
.ev-contradicts { background:#fef2f2; color:#7f1d1d; }
.ev-complicates { background:#fffbeb; color:#78350f; }
.ev-date { font-size: 11px; color: #9dc4d8; margin-left: auto; }
.ev-card-title { font-size: 14px; font-weight: 600; color: #071828; margin-bottom: 6px; line-height: 1.4; }
.ev-reason { font-size: 12px; color: #0a7d8c; margin-bottom: 8px;
  font-style: italic; }
.ev-excerpt { font-size: 13px; color: #274d68; line-height: 1.6; margin-bottom: 12px;
  border-left: 2px solid #cde0ea; padding-left: 10px; }
.ev-foot { display: flex; justify-content: space-between; align-items: center; }
.ev-source-url { font-size: 11px; color: #9dc4d8; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="ev-header">
  <div class="ev-eyebrow">Evidence First</div>
  <div class="ev-title">What's your hunch?</div>
  <div class="ev-sub">Type a hypothesis — the system finds what confirms, contradicts, or complicates it.</div>
</div>
""", unsafe_allow_html=True)

# ── Input ──────────────────────────────────────────────────────────────────────
_ev_hunch = st.text_area(
    "Your hypothesis",
    placeholder="e.g. Desk lunch became a symbol of corporate resignation, not productivity",
    height=80,
    label_visibility="collapsed",
    key="ev_hunch",
)

_ev_col1, _ev_col2 = st.columns([3, 1])
with _ev_col1:
    _ev_sources = st.multiselect(
        "Sources",
        ["Saved signals", "Reddit (live)", "RSS (live)", "GDELT (live)",
         "Google Trends (live)", "Hacker News (live)",
         "YouTube (live)", "TikTok (live)", "Instagram (live)", "X/Twitter (live)"],
        default=["Saved signals"],
        label_visibility="collapsed",
        key="ev_sources",
    )
with _ev_col2:
    _ev_run = st.button("Find evidence →", use_container_width=True, key="ev_run",
                        type="primary")

# ── Run Evidence Search ────────────────────────────────────────────────────────
if _ev_run and _ev_hunch.strip():
    _ev_api_key     = os.environ.get("ANTHROPIC_API_KEY", "")
    _ev_apify_key   = os.environ.get("APIFY_API_TOKEN", "")
    _ev_youtube_key = os.environ.get("YOUTUBE_API_KEY", "")
    _ev_exa_key     = os.environ.get("EXA_API_KEY", "")

    _ev_raw: list[dict] = []
    _ev_status = st.empty()

    # ── Extract 3-4 key search words from the hunch (stop-word stripped)
    # Full natural-language phrases choke Reddit/GDELT/HN APIs; short keywords work.
    _ev_stops = {
        "the","and","but","for","at","their","to","of","in","a","an","is","are",
        "was","were","be","been","have","has","had","do","does","did","will",
        "would","could","should","may","might","i","you","he","she","it","we",
        "they","what","which","who","when","where","why","how","all","so","than",
        "too","very","just","as","if","by","or","also","with","from","on","off",
        "over","under","again","then","here","there","not","no","only","same",
        "such","even","into","about","than","people","avoid","fear","this","that",
    }
    _ev_kws = [
        w for w in _re_global.sub(r"[^\w\s]", "", _ev_hunch.lower()).split()
        if len(w) > 3 and w not in _ev_stops
    ][:4]
    _ev_search = " ".join(_ev_kws) if _ev_kws else _ev_hunch[:60]

    # 1. Saved signals from DB — lenient: match ANY 1 keyword
    if "Saved signals" in _ev_sources:
        _ev_status.caption("Searching signal database…")
        _all_sigs = load_signals(limit=500)
        _ev_kw_set = set(_ev_kws) if _ev_kws else {w for w in _ev_hunch.lower().split() if len(w) > 3}
        for _s in _all_sigs:
            _text = f"{_s.get('title','')} {_s.get('content','')}".lower()
            if any(w in _text for w in _ev_kw_set):
                _ev_raw.append(_s)
        _ev_raw = _ev_raw[:40]

    # 2. Live scrapes — use short keyword query for all text-search APIs
    try:
        from ingestion import (scrape_reddit, scrape_rss, scrape_gdelt,
                               scrape_google_trends, scrape_hacker_news,
                               scrape_youtube, scrape_tiktok, scrape_instagram,
                               scrape_twitter)

        def _ev_cb(msg): _ev_status.caption(msg)

        if "Reddit (live)" in _ev_sources:
            _ev_status.caption(f"[Reddit] Searching '{_ev_search}'…")
            for s in scrape_reddit(_ev_search, max_items=15, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

        if "RSS (live)" in _ev_sources:
            _ev_status.caption("Reading RSS feeds…")
            for s in scrape_rss(max_items_per_feed=4, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

        if "GDELT (live)" in _ev_sources:
            _ev_status.caption(f"[GDELT] Searching '{_ev_search}'…")
            for s in scrape_gdelt(_ev_search, n=15, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

        if "Google Trends (live)" in _ev_sources:
            _ev_status.caption(f"[Google Trends] Searching '{_ev_search}'…")
            for s in scrape_google_trends(_ev_search, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

        if "Hacker News (live)" in _ev_sources:
            _ev_status.caption(f"[Hacker News] Searching '{_ev_search}'…")
            for s in scrape_hacker_news(_ev_search, n=10, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

        if "YouTube (live)" in _ev_sources and _ev_youtube_key:
            # Search each keyword individually — YouTube API ignores multi-word phrases
            _yt_terms = _ev_kws[:3] if len(_ev_kws) > 1 else [_ev_search]
            _yt_seen_urls: set = set()
            for _yt_kw in _yt_terms:
                _ev_status.caption(f"[YouTube] Searching '{_yt_kw}' (GB)…")
                for s in scrape_youtube(_yt_kw, api_key=_ev_youtube_key,
                                        n=6, region_code="GB", callback=_ev_cb):
                    if s.url not in _yt_seen_urls:
                        _yt_seen_urls.add(s.url)
                        _ev_raw.append({"title": s.title, "content": s.content,
                                        "thumbnail": (s.raw_meta or {}).get("thumbnail", ""),
                                        "source": s.source, "url": s.url,
                                        "timestamp": s.timestamp})

        if "TikTok (live)" in _ev_sources and _ev_apify_key:
            _ev_status.caption(f"[TikTok] Searching '{_ev_search}' via Apify…")
            for s in scrape_tiktok(_ev_search, api_token=_ev_apify_key, n=15,
                                   fetch_comments=False, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content,
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", ""),
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

        if "Instagram (live)" in _ev_sources and _ev_apify_key:
            _ev_status.caption(f"[Instagram] Searching '{_ev_search}' via Apify…")
            for s in scrape_instagram(_ev_search, api_token=_ev_apify_key, n=15, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content,
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", ""),
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

        if "X/Twitter (live)" in _ev_sources and _ev_apify_key:
            _ev_status.caption(f"[X/Twitter] Searching '{_ev_search}' via Apify…")
            for s in scrape_twitter(_ev_search, api_token=_ev_apify_key, n=15, callback=_ev_cb):
                _ev_raw.append({"title": s.title, "content": s.content, "thumbnail": "",
                                "source": s.source, "url": s.url, "timestamp": s.timestamp})

    except Exception as _ev_scrape_exc:
        st.warning(f"Some live sources failed: {_ev_scrape_exc}")

    # Show what was actually searched so user can see the term extraction
    st.caption(f"🔎 Searching as: **{_ev_search}** · {len(_ev_raw)} signals collected")

    # 3. Claude classifies signals
    _ev_status.caption(f"Claude classifying {len(_ev_raw)} signals…")
    _ev_results: list[dict] = []

    if _ev_raw and _ev_api_key:
        try:
            import anthropic as _ant_ev
            _ev_client = _ant_ev.Anthropic(api_key=_ev_api_key)
            _ev_batch = _ev_raw[:30]  # cap to keep prompt manageable
            _ev_signals_txt = "\n\n".join(
                f"[{i}] SOURCE: {s.get('source','?')} | URL: {s.get('url','')}\n"
                f"TITLE: {s.get('title','')[:120]}\n"
                f"CONTENT: {s.get('content','')[:300]}"
                for i, s in enumerate(_ev_batch)
            )
            _ev_prompt = f"""You are an evidence analyst. The user has a hunch:

HUNCH: "{_ev_hunch}"

Below are {len(_ev_batch)} signals from various sources. For each signal, classify it as one of:
- CONFIRMS: directly supports the hunch
- CONTRADICTS: challenges or disproves the hunch
- COMPLICATES: adds nuance, a counter-example, or a related tension that complicates the picture

For each signal, respond with a JSON array entry. Keep "reason" to one short sentence (max 15 words).

SIGNALS:
{_ev_signals_txt}

Respond ONLY with a valid JSON array like:
[
  {{"index": 0, "verdict": "CONFIRMS", "reason": "Workers report eating at desks from resignation, not efficiency."}},
  ...
]
Include all {len(_ev_batch)} entries."""

            _ev_resp = _ev_client.messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
                max_tokens=2048,
                messages=[{"role": "user", "content": _ev_prompt}],
            )
            _ev_txt = _ev_resp.content[0].text.strip()
            # Extract JSON array
            _ev_json_start = _ev_txt.find("[")
            _ev_json_end   = _ev_txt.rfind("]") + 1
            if _ev_json_start >= 0 and _ev_json_end > _ev_json_start:
                _ev_classifications = json.loads(_ev_txt[_ev_json_start:_ev_json_end])
                for _cl in _ev_classifications:
                    _idx = _cl.get("index", -1)
                    if 0 <= _idx < len(_ev_batch):
                        _sig = dict(_ev_batch[_idx])
                        _sig["_verdict"] = _cl.get("verdict", "COMPLICATES").upper()
                        _sig["_reason"]  = _cl.get("reason", "")
                        _ev_results.append(_sig)
        except Exception as _ev_cls_exc:
            st.warning(f"Classification failed: {_ev_cls_exc}")
            # Fallback: show results without classification
            for _s in _ev_raw[:30]:
                _s2 = dict(_s)
                _s2["_verdict"] = "COMPLICATES"
                _s2["_reason"] = ""
                _ev_results.append(_s2)
    elif _ev_raw:
        for _s in _ev_raw[:30]:
            _s2 = dict(_s)
            _s2["_verdict"] = "COMPLICATES"
            _s2["_reason"] = ""
            _ev_results.append(_s2)

    st.session_state["ev_results"] = _ev_results
    st.session_state["ev_hunch_used"] = _ev_hunch
    _ev_status.empty()

elif not _ev_run:
    pass  # first load — no results yet

# ── Results ────────────────────────────────────────────────────────────────────
_ev_results_stored = st.session_state.get("ev_results", [])
_ev_hunch_stored   = st.session_state.get("ev_hunch_used", "")

if _ev_results_stored:
    _ev_confirms    = sum(1 for r in _ev_results_stored if r.get("_verdict") == "CONFIRMS")
    _ev_contradicts = sum(1 for r in _ev_results_stored if r.get("_verdict") == "CONTRADICTS")
    _ev_complicates = sum(1 for r in _ev_results_stored if r.get("_verdict") == "COMPLICATES")

    # Filter chips
    _ev_filter_col, _ev_sort_col = st.columns([4, 1])
    with _ev_filter_col:
        _ev_filter = st.radio(
            "Filter",
            ["All", f"Confirms ({_ev_confirms})",
             f"Contradicts ({_ev_contradicts})",
             f"Complicates ({_ev_complicates})"],
            horizontal=True,
            label_visibility="collapsed",
            key="ev_filter",
        )

    # Apply filter
    _ev_show = _ev_results_stored
    if _ev_filter.startswith("Confirms"):
        _ev_show = [r for r in _ev_results_stored if r.get("_verdict") == "CONFIRMS"]
    elif _ev_filter.startswith("Contradicts"):
        _ev_show = [r for r in _ev_results_stored if r.get("_verdict") == "CONTRADICTS"]
    elif _ev_filter.startswith("Complicates"):
        _ev_show = [r for r in _ev_results_stored if r.get("_verdict") == "COMPLICATES"]

    st.caption(f"{len(_ev_show)} signals · hunch: *{_ev_hunch_stored[:80]}*")
    st.markdown("---")

    _ev_src_class = {
        "reddit": "ev-src-reddit", "rss": "ev-src-rss", "gdelt": "ev-src-gdelt",
        "google_trends": "ev-src-trends", "hacker_news": "ev-src-hn",
        "youtube": "ev-src-youtube", "tiktok": "ev-src-tiktok",
        "instagram": "ev-src-instagram", "twitter": "ev-src-twitter",
    }
    _ev_verdict_class = {
        "CONFIRMS": "ev-confirms", "CONTRADICTS": "ev-contradicts", "COMPLICATES": "ev-complicates",
    }
    _ev_verdict_emoji = {"CONFIRMS": "✓", "CONTRADICTS": "✗", "COMPLICATES": "~"}

    for _ev_i, _ev_r in enumerate(_ev_show):
        _src      = _ev_r.get("source", "?")
        _verdict  = _ev_r.get("_verdict", "COMPLICATES")
        _reason   = _ev_r.get("_reason", "")
        _title    = _ev_r.get("title", "")[:120]
        _content  = _ev_r.get("content", "")[:280]
        _url      = _ev_r.get("url", "")
        _ts       = (_ev_r.get("timestamp") or "")[:10]
        _src_cls  = _ev_src_class.get(_src, "ev-src-rss")
        _vrd_cls  = _ev_verdict_class.get(_verdict, "ev-complicates")
        _vrd_lbl  = f"{_ev_verdict_emoji.get(_verdict,'')} {_verdict.capitalize()}"
        _domain   = urllib.parse.urlparse(_url).netloc if _url else _src
        # Thumbnail — start invisible, fade in only on successful load, stay hidden on error
        _raw_thumb = _ev_r.get("thumbnail", "") or ""
        _thumb_html = ""
        if _raw_thumb:
            _prx = _tr_proxy_thumb(_raw_thumb)
            _is_social = _src in ("instagram", "tiktok")
            _grad = ("linear-gradient(135deg,#6a1f6e,#c94f35,#e8a020)"
                     if _is_social else "linear-gradient(135deg,#1a3d52,#0fa3b5)")
            _thumb_html = (
                f'<div style="margin-bottom:10px;border-radius:8px;overflow:hidden;'
                f'height:140px;background:{_grad};position:relative;">'
                # Fallback star shown when image fails (same aesthetic as Research Lab cards)
                f'<div style="position:absolute;inset:0;display:flex;align-items:center;'
                f'justify-content:center;color:rgba(255,255,255,.3);font-size:28px;">✦</div>'
                # Image: invisible until loaded, hides itself on error
                f'<img src="{_prx}" loading="lazy" '
                f'style="position:absolute;inset:0;width:100%;height:100%;'
                f'object-fit:cover;opacity:0;transition:opacity .4s;" '
                f'onload="this.style.opacity=1" '
                f'onerror="this.style.display=\'none\'"/>'
                f'</div>'
            )

        st.markdown(f"""
<div class="ev-card">
  {_thumb_html}
  <div class="ev-card-top">
    <span class="ev-src {_src_cls}">{e(_src.replace('_',' '))}</span>
    <span class="ev-verdict {_vrd_cls}">{_vrd_lbl}</span>
    <span class="ev-date">{_ts}</span>
  </div>
  <div class="ev-card-title">{e(_title)}</div>
  {"<div class='ev-reason'>" + e(_reason) + "</div>" if _reason else ""}
  <div class="ev-excerpt">{e(_content)}{"…" if len(_ev_r.get('content','')) > 280 else ""}</div>
  <div class="ev-foot">
    <span class="ev-source-url">{e(_domain)}</span>
  </div>
</div>""", unsafe_allow_html=True)

        _ev_btn_col1, _ev_btn_col2 = st.columns([6, 1])
        with _ev_btn_col2:
            if st.button("+ Save", key=f"ev_save_{_ev_i}", use_container_width=True):
                _ev_user = st.session_state.get("logged_in_user", "internal")
                add_curadoria_item(
                    _ev_user, _src, _title,
                    _ev_r.get("content", "")[:1000],
                )
                st.success("Saved to board!")

elif _ev_run and not _ev_hunch.strip():
    st.warning("Please enter a hunch first.")
else:
    st.markdown("""
<div style="text-align:center; padding: 4rem 2rem; color: #9dc4d8;">
  <div style="font-size: 2rem; margin-bottom: 1rem;">◎</div>
  <div style="font-size: 14px; font-family: Georgia, serif;">
    Type a hypothesis above and click <em>Find evidence</em>
  </div>
  <div style="font-size: 12px; margin-top: 8px; font-family: monospace; letter-spacing: .06em; text-transform: uppercase;">
    The system will search your signals and classify what confirms, contradicts, or complicates your thinking
  </div>
</div>""", unsafe_allow_html=True)

_tr_ctr_evidence.__exit__(None, None, None)
_tr_ctr_research.__enter__()

# ══════════════════════════════════════════════════════════════════════════════
# TRENDS BOARD — visual kanban of trending topics by velocity
# v2: English only, brand-term expansion, saved-signals pre-load,
#     higher limits, clickable links, velocity chart below board.
# ══════════════════════════════════════════════════════════════════════════════

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Trends Board column wrappers ── */
.tr-col-wrap { border-radius: 12px; padding: 14px 12px; min-height: 120px; }
.tr-col-high    { background: #f0fdf4; border-top: 3px solid #16a34a; }
.tr-col-stable  { background: #eff6ff; border-top: 3px solid #2563eb; }
.tr-col-decline { background: #fff7ed; border-top: 3px solid #ea580c; }
.tr-col-header  { font-size: 11px; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; margin-bottom: 14px; padding-bottom: 8px;
  border-bottom: 1px solid rgba(0,0,0,.06); }
.tr-col-high .tr-col-header    { color: #16a34a; }
.tr-col-stable .tr-col-header  { color: #2563eb; }
.tr-col-decline .tr-col-header { color: #ea580c; }

/* ── Trend cards ── */
.tr-card { background: #fff; border-radius: 9px; padding: 12px 13px;
  margin-bottom: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.07);
  border: 0.5px solid rgba(0,0,0,.06); }
.tr-card-top { display: flex; gap: 6px; align-items: center;
  margin-bottom: 7px; flex-wrap: wrap; }
.tr-src { font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
  padding: 2px 8px; border-radius: 4px; font-weight: 700; }
/* source badge colours — reuse Evidence palette */
.tr-src-reddit        { background:#fdefd5; color:#7a4a0a; }
.tr-src-google_trends { background:#fce7f3; color:#831843; }
.tr-src-hacker_news   { background:#fff3e0; color:#7c4a00; }
.tr-src-youtube       { background:#fce8e8; color:#8b0000; }
.tr-src-tiktok        { background:#e8f5e9; color:#1b5e20; }
.tr-src-instagram     { background:#f3e5f5; color:#4a148c; }
.tr-src-twitter       { background:#e7f3ff; color:#003566; }
.tr-src-gdelt         { background:#ede9fe; color:#4c3494; }
.tr-src-rss           { background:#e4f4f5; color:#0a5560; }
.tr-src-exa           { background:#fef9c3; color:#713f12; }

/* velocity badge */
.tr-vel { font-size: 11px; font-weight: 700; margin-left: auto; }
.tr-vel-high    { color: #16a34a; }
.tr-vel-stable  { color: #2563eb; }
.tr-vel-decline { color: #ea580c; }

.tr-card-name { font-size: 13px; font-weight: 600; color: #071828;
  line-height: 1.4; margin-bottom: 4px; }
.tr-card-note { font-size: 11px; color: #6ea8c4; line-height: 1.4; }

/* dimension tags */
.tr-dims { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 8px; }
.tr-dim { font-size: 9.5px; font-weight: 600; letter-spacing: .05em;
  text-transform: uppercase; padding: 2px 7px; border-radius: 20px; }
.tr-dim-emotion { background: #fce7f3; color: #831843; }
.tr-dim-hook    { background: #fef9c3; color: #713f12; }
.tr-dim-tone    { background: #e0f2fe; color: #0c4a6e; }

/* ── Social source placeholders (shown when CDN blocks the real thumbnail) ── */
.tr-thumb-wrap { position: relative; width: 100%; height: 120px; border-radius: 6px;
  margin-bottom: 8px; overflow: hidden; }
.tr-thumb-wrap img { position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  object-fit: cover; z-index: 2; transition: opacity .15s; }
.tr-ph-tiktok { background: linear-gradient(135deg, #010101 30%, #1a1a2e 100%);
  display: flex; align-items: center; justify-content: center; }
.tr-ph-tiktok::before { content: "♪"; font-size: 2.2rem;
  color: rgba(255,255,255,.3); position: absolute; z-index: 1; }
.tr-ph-instagram { background: linear-gradient(135deg, #833ab4 0%, #fd1d1d 50%, #fcb045 100%);
  display: flex; align-items: center; justify-content: center; }
.tr-ph-instagram::before { content: "✦"; font-size: 2rem;
  color: rgba(255,255,255,.35); position: absolute; z-index: 1; }

/* ── Quantitative Overview ── */
.ov-wrap { background: #fff; border-radius: 14px; padding: 20px 22px;
  margin-bottom: 20px; border: 1px solid rgba(0,0,0,.07);
  box-shadow: 0 2px 10px rgba(0,0,0,.05); }
.ov-section-label { font-size: 9.5px; font-weight: 700; letter-spacing: .14em;
  text-transform: uppercase; color: #9dc4d8; margin-bottom: 10px; }
/* sentiment bar */
.ov-sent-bar { display: flex; border-radius: 6px; overflow: hidden;
  height: 20px; margin-bottom: 8px; gap: 2px; }
.ov-sent-seg { display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 700; color: #fff; transition: width .3s;
  min-width: 28px; }
.ov-sent-pos { background: #16a34a; }
.ov-sent-neg { background: #dc2626; }
.ov-sent-neu { background: #94a3b8; }
.ov-sent-legend { display: flex; gap: 14px; margin-bottom: 6px; }
.ov-sent-leg-item { display: flex; align-items: center; gap: 5px;
  font-size: 11px; color: #4a6d82; }
.ov-sent-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.ov-sent-summary { font-size: 12px; color: #6ea8c4; font-style: italic;
  margin-top: 4px; }
/* themes */
.ov-themes { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 4px; }
.ov-theme-chip { display: flex; align-items: center; gap: 5px;
  border-radius: 20px; padding: 4px 10px; font-size: 11px; font-weight: 600; }
.ov-theme-pos { background: #dcfce7; color: #15803d; }
.ov-theme-neg { background: #fee2e2; color: #b91c1c; }
.ov-theme-neu { background: #f1f5f9; color: #475569; }
.ov-theme-count { font-size: 10px; font-weight: 700; opacity: .7; }
/* source pills */
.ov-sources { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.ov-src-pill { border-radius: 20px; padding: 3px 9px; font-size: 10px;
  font-weight: 700; letter-spacing: .05em; }

/* ── Strategic Opening cards ── */
.so-card { background: #fff; border-radius: 14px; padding: 22px 22px 16px;
  margin-bottom: 18px; box-shadow: 0 2px 12px rgba(0,0,0,.07);
  border: 1px solid rgba(0,0,0,.07); border-left: 4px solid #0a7d8c; }
.so-card-now      { border-left-color: #16a34a; }
.so-card-emerging { border-left-color: #d97706; }
.so-card-building { border-left-color: #2563eb; }
.so-header { display: flex; align-items: flex-start; justify-content: space-between;
  gap: 10px; margin-bottom: 8px; }
.so-tension { font-family: Georgia, serif; font-size: 1.2rem; font-weight: 700;
  color: #071828; line-height: 1.3; flex: 1; }
.so-urgency { font-size: 10px; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; padding: 3px 9px; border-radius: 20px;
  white-space: nowrap; flex-shrink: 0; margin-top: 4px; }
.so-urgency-now      { background: #dcfce7; color: #15803d; }
.so-urgency-emerging { background: #fef3c7; color: #b45309; }
.so-urgency-building { background: #dbeafe; color: #1d4ed8; }
.so-why-now { font-size: 13px; color: #4a6d82; line-height: 1.6;
  margin-bottom: 14px; }
.so-signals-label { font-size: 9.5px; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; color: #9dc4d8; margin-bottom: 8px; }
.so-signal { border-left: 2px solid #e2ecf0; padding: 5px 0 5px 10px;
  margin-bottom: 8px; }
.so-signal-quote { font-size: 12px; color: #274d68; line-height: 1.55;
  font-style: italic; margin-bottom: 4px; }
.so-signal-meta { display: flex; gap: 6px; align-items: center; }
.so-angle-wrap { background: #f0f7fb; border-radius: 8px; padding: 10px 13px;
  margin: 14px 0 4px; border: 1px dashed #9dc4d8; }
.so-angle-label { font-size: 9.5px; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; color: #0a7d8c; margin-bottom: 4px; }
.so-angle-text { font-size: 13px; color: #071828; line-height: 1.5; }
.so-hook { font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 4px;
  background: #fef9c3; color: #713f12; display: inline-block; margin-top: 5px; }

/* ── Hunch suggestions ── */
.hn-sugg-wrap { margin-bottom: 10px; }
.hn-sugg-label { font-size: 9.5px; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; color: #7c3aed; margin-bottom: 6px; }
.hn-sugg-chip { display: inline-block; background: #f5f3ff; color: #4c3494;
  border: 1px solid #c4b5fd; border-radius: 20px; padding: 4px 11px;
  font-size: 11px; margin: 0 5px 5px 0; cursor: pointer; }

/* ── Heat Map ── */
.hm-wrap { margin-bottom: 24px; }
.hm-legend { display: flex; align-items: center; gap: 10px;
  margin-bottom: 10px; }
.hm-legend-bar { flex: 1; height: 8px; border-radius: 4px;
  background: linear-gradient(to right, #3b82f6, #8b5cf6, #f97316); }
.hm-legend-label { font-size: 10px; color: #9dc4d8;
  font-family: monospace; letter-spacing: .06em; white-space: nowrap; }
.hm-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.hm-tile { border-radius: 9px; padding: 10px 12px; cursor: default;
  transition: transform .12s; border-width: 1.5px; border-style: solid; }
.hm-tile:hover { transform: translateY(-1px); }
.hm-tile-cat { font-size: 9px; font-weight: 700; letter-spacing: .1em;
  text-transform: uppercase; margin-bottom: 4px; }
.hm-tile-name { font-size: 11px; font-weight: 600; color: #071828;
  line-height: 1.35; }
/* confirms = warm/hot */
.hm-hot-1 { background: #fff7ed; border-color: #f97316; }
.hm-hot-1 .hm-tile-cat { color: #c2410c; }
.hm-hot-2 { background: #fff7f0; border-color: #fb923c; }
.hm-hot-2 .hm-tile-cat { color: #c2410c; }
.hm-hot-3 { background: #fffbf5; border-color: #fdba74; }
.hm-hot-3 .hm-tile-cat { color: #c2410c; }
/* unexpected = purple/medium */
.hm-mid-1 { background: #f5f3ff; border-color: #8b5cf6; }
.hm-mid-1 .hm-tile-cat { color: #6d28d9; }
.hm-mid-2 { background: #f8f5ff; border-color: #a78bfa; }
.hm-mid-2 .hm-tile-cat { color: #6d28d9; }
.hm-mid-3 { background: #faf8ff; border-color: #c4b5fd; }
.hm-mid-3 .hm-tile-cat { color: #6d28d9; }
/* challenges = cool/cold */
.hm-cold-1 { background: #eff6ff; border-color: #3b82f6; }
.hm-cold-1 .hm-tile-cat { color: #1d4ed8; }
.hm-cold-2 { background: #f0f7ff; border-color: #60a5fa; }
.hm-cold-2 .hm-tile-cat { color: #1d4ed8; }
.hm-cold-3 { background: #f5faff; border-color: #93c5fd; }
.hm-cold-3 .hm-tile-cat { color: #1d4ed8; }

/* ── Hunch mode ── */
.hn-col-wrap { border-radius: 12px; padding: 14px 12px; min-height: 120px; }
.hn-col-confirms   { background: #f0fdf4; border-top: 3px solid #16a34a; }
.hn-col-challenges { background: #fff7ed; border-top: 3px solid #ea580c; }
.hn-col-unexpected { background: #f5f3ff; border-top: 3px solid #7c3aed; }
.hn-col-header { font-size: 11px; font-weight: 700; letter-spacing: .12em;
  text-transform: uppercase; margin-bottom: 14px; padding-bottom: 8px;
  border-bottom: 1px solid rgba(0,0,0,.06); }
.hn-col-confirms .hn-col-header   { color: #16a34a; }
.hn-col-challenges .hn-col-header { color: #ea580c; }
.hn-col-unexpected .hn-col-header { color: #7c3aed; }
.hn-card { background: #fff; border-radius: 9px; padding: 12px 13px;
  margin-bottom: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.07);
  border: 0.5px solid rgba(0,0,0,.06); }
.hn-card-top { display: flex; align-items: center; margin-bottom: 7px; gap: 6px; }
.hn-card-name { font-size: 13px; font-weight: 600; color: #071828;
  line-height: 1.4; margin-bottom: 5px; }
.hn-card-quote { font-size: 11px; color: #4a6d82; line-height: 1.55;
  font-style: italic; border-left: 2px solid #cde0ea;
  padding-left: 9px; margin: 6px 0 7px; }
.hn-card-note { font-size: 11px; color: #6ea8c4; line-height: 1.4; }
.hn-rel { font-size: 11px; font-weight: 700; margin-left: auto; white-space: nowrap; }
.hn-rel-confirms   { color: #16a34a; }
.hn-rel-challenges { color: #ea580c; }
.hn-rel-unexpected { color: #7c3aed; }
.hn-input-wrap { background: #f0f7fb; border: 1.5px dashed #9dc4d8;
  border-radius: 12px; padding: 14px 16px; margin: 10px 0 6px; }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div style="padding:1.2rem 0 .8rem;text-align:center;">
  <div style="font-family:monospace;font-size:10px;letter-spacing:.18em;
    text-transform:uppercase;color:#6ea8c4;margin-bottom:6px;">Research Lab</div>
  <div style="font-family:Georgia,serif;font-size:1.4rem;font-weight:400;
    color:#071828;margin-bottom:3px;">What does the evidence actually say?</div>
  <div style="font-size:12px;color:#6ea8c4;">
    Start with a hypothesis — Claude maps what confirms, challenges, and surprises.
  </div>
</div>
""", unsafe_allow_html=True)

# ── PRIMARY: Hunch input ──────────────────────────────────────────────────────
st.markdown(
    '<div style="font-size:1.15rem;font-weight:700;color:#071828;margin:6px 0 2px;">'
    '💡 Test a Hypothesis</div>'
    '<div style="font-size:12px;color:#6ea8c4;margin-bottom:10px;">'
    'Type a hunch — Claude searches your sources and maps what confirms, challenges, and surprises.</div>',
    unsafe_allow_html=True,
)
# Auto-suggested hunches (populated after Find Openings)
_tr_hunch_suggestions = st.session_state.get("tr_hunch_suggestions", [])
if _tr_hunch_suggestions:
    st.markdown('<div class="hn-sugg-label" style="margin-bottom:6px;">Suggested hypotheses — click to test</div>', unsafe_allow_html=True)
    _sugg_cols = st.columns(len(_tr_hunch_suggestions))
    for _si, _sugg in enumerate(_tr_hunch_suggestions):
        with _sugg_cols[_si]:
            if st.button(f"→ {_sugg[:65]}", key=f"hn_sugg_{_si}",
                         use_container_width=True, help="Click to test this hypothesis"):
                st.session_state["tr_hunch_prefill"] = _sugg
                st.rerun()
_hn_col1, _hn_col2 = st.columns([4, 1])
with _hn_col1:
    _hunch_prefill = st.session_state.pop("tr_hunch_prefill", None)
    if _hunch_prefill:
        st.session_state["tr_hunch"] = _hunch_prefill
    _tr_hunch = st.text_input(
        "hunch", label_visibility="collapsed",
        placeholder='e.g. "People avoid soup at their desk because they fear spilling on their laptop"',
        key="tr_hunch",
    )
with _hn_col2:
    _tr_hunch_fetch = st.button("Find Evidence →", key="tr_hunch_fetch",
                                type="primary", use_container_width=True)

# Sources multiselect (shared)
_tr_sources = st.multiselect(
    "Sources to scan",
    ["Saved signals", "Google Trends", "Reddit", "Hacker News", "GDELT", "RSS",
     "YouTube", "TikTok", "Instagram", "X/Twitter", "Exa"],
    default=["Saved signals", "Reddit", "Instagram", "X/Twitter", "YouTube", "TikTok"],
    key="tr_sources", label_visibility="collapsed",
)

# ── SECONDARY: Strategic Openings topic input ─────────────────────────────────
with st.expander("🔭 Find Strategic Openings — What can this brand do with these signals?",
                 expanded=False):
    st.caption("Enter a brand or topic to surface 3 specific creative opportunities from your sources.")
    _tr_c1, _tr_c2 = st.columns([3, 1])
    with _tr_c1:
        _tr_topic = st.text_input(
            "Topic", placeholder="e.g. Heinz, desk lunch, quiet quitting",
            key="tr_topic", label_visibility="collapsed",
        )
    with _tr_c2:
        _tr_fetch = st.button("Find Openings →", key="tr_fetch",
                              use_container_width=True)

# ── Fetch & classify ──────────────────────────────────────────────────────────
if _tr_fetch and _tr_topic.strip():
    _tr_apify_key   = os.environ.get("APIFY_API_TOKEN", "")
    _tr_youtube_key = os.environ.get("YOUTUBE_API_KEY", "")
    _tr_exa_key     = os.environ.get("EXA_API_KEY", "")
    _tr_ant_key     = os.environ.get("ANTHROPIC_API_KEY", "")

    _tr_raw: list[dict] = []   # {"title","content","source","url"}
    _tr_status = st.empty()

    def _tr_set_status(msg: str) -> None:
        """Render a styled loading bar with animated dots."""
        _tr_status.markdown(
            f'<style>'
            f'@keyframes _lhblink{{0%,100%{{opacity:.15;}}50%{{opacity:1;}}}}'
            f'._lhd1{{display:inline-block;animation:_lhblink 1.2s 0s infinite;}}'
            f'._lhd2{{display:inline-block;animation:_lhblink 1.2s .4s infinite;}}'
            f'._lhd3{{display:inline-block;animation:_lhblink 1.2s .8s infinite;}}'
            f'</style>'
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:11.5px;'
            f'color:#0a5560;background:#e4f4f5;border-left:3px solid #0fa3b5;'
            f'border-radius:0 6px 6px 0;padding:7px 12px;margin:4px 0;">'
            f'{msg}'
            f'<span class="_lhd1">.</span><span class="_lhd2">.</span><span class="_lhd3">.</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # Step 0 — Brand expansion: use Claude to derive related search terms
    _tr_expanded_terms = [_tr_topic]
    if _tr_ant_key:
        try:
            import anthropic as _ant_tr0
            _tr_set_status(f"Claude expanding search terms for '{_tr_topic}'…")
            _tr_exp_resp = _ant_tr0.Anthropic(api_key=_tr_ant_key).messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
                max_tokens=200,
                messages=[{"role": "user", "content":
                    f'Give 4 closely related search terms for "{_tr_topic}" '
                    f'(brand, products, competitors, culture). '
                    f'JSON array of strings only, no explanation.'}],
            )
            _tr_exp_txt = _tr_exp_resp.content[0].text.strip()
            _tr_exp_js  = _tr_exp_txt[_tr_exp_txt.find("["):_tr_exp_txt.rfind("]")+1]
            _tr_expanded_terms += json.loads(_tr_exp_js)
        except Exception:
            pass
    _tr_set_status(f"Searching for: {', '.join(_tr_expanded_terms[:5])}")

    try:
        from ingestion import (
            scrape_google_trends, scrape_reddit, scrape_hacker_news,
            scrape_gdelt, scrape_rss, scrape_youtube,
            scrape_tiktok, scrape_instagram, scrape_twitter, scrape_exa,
        )
        _tr_log: list[str] = []  # collect all status messages for debug
        def _tr_cb(msg):
            _tr_set_status(msg)
            _tr_log.append(msg)

        # Saved signals — keyword search across DB
        if "Saved signals" in _tr_sources:
            _tr_set_status("Searching saved signals…")
            _all_db = load_signals(limit=500)
            _q_words = set(_tr_topic.lower().split())
            for _s in _all_db:
                _txt = f"{_s.get('title','')} {_s.get('content','')}".lower()
                if any(w in _txt for w in _q_words if len(w) > 2):
                    _tr_raw.append({"title": _s.get("title",""), "content": _s.get("content","")[:300],
                                    "source": _s.get("source","rss"), "url": _s.get("url","")})
            _tr_raw = _tr_raw[:60]

        # Live sources — iterate over expanded terms for broader coverage
        for _term in _tr_expanded_terms[:3]:
            if "Google Trends" in _tr_sources:
                for s in scrape_google_trends(_term, callback=_tr_cb):
                    _tr_raw.append({"title": s.title, "content": s.content,
                                    "source": s.source, "url": s.url or ""})

            if "Reddit" in _tr_sources:
                for s in scrape_reddit(_term, max_items=25, callback=_tr_cb):
                    _tr_raw.append({"title": s.title, "content": s.content[:300],
                                    "source": s.source, "url": s.url or ""})

            if "Hacker News" in _tr_sources:
                for s in scrape_hacker_news(_term, n=20, callback=_tr_cb):
                    _tr_raw.append({"title": s.title, "content": s.content[:300],
                                    "source": s.source, "url": s.url or ""})

            if "GDELT" in _tr_sources:
                for s in scrape_gdelt(_term, n=20, callback=_tr_cb):
                    _tr_raw.append({"title": s.title, "content": s.content[:300],
                                    "source": s.source, "url": s.url or ""})

        if "RSS" in _tr_sources:
            for s in scrape_rss(max_items_per_feed=5, callback=_tr_cb):
                _tr_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or ""})

        if "YouTube" in _tr_sources and _tr_youtube_key:
            _tr_yt_seen: set = set()
            for _term in _tr_expanded_terms[:3]:
                _tr_set_status(f"[YouTube] Searching '{_term}' (GB)…")
                for s in scrape_youtube(_term, api_key=_tr_youtube_key,
                                        n=8, region_code="GB", callback=_tr_cb):
                    if s.url not in _tr_yt_seen:
                        _tr_yt_seen.add(s.url)
                        _tr_raw.append({"title": s.title, "content": s.content[:300],
                                        "source": s.source, "url": s.url or "",
                                        "thumbnail": (s.raw_meta or {}).get("thumbnail", "")})

        if "TikTok" in _tr_sources and _tr_apify_key:
            _tt_before = len(_tr_raw)
            _tr_set_status(f"[TikTok] Searching '{_tr_topic}' via Apify…")
            for s in scrape_tiktok(_tr_topic, api_token=_tr_apify_key,
                                   n=30, fetch_comments=False, callback=_tr_cb):
                _tr_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "",
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", "")})
            _tt_count = len(_tr_raw) - _tt_before
            if _tt_count == 0:
                _tr_set_status("⚠️ TikTok returned 0 results — trying without comments…")
        elif "TikTok" in _tr_sources and not _tr_apify_key:
            st.warning("TikTok selected but APIFY_API_TOKEN is not set.")

        if "Instagram" in _tr_sources and _tr_apify_key:
            _ig_before = len(_tr_raw)
            _tr_set_status(f"[Instagram] Searching '{_tr_topic}' via Apify…")
            for s in scrape_instagram(_tr_topic, api_token=_tr_apify_key,
                                      n=30, callback=_tr_cb):
                _tr_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "",
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", "")})
            _ig_count = len(_tr_raw) - _ig_before
            if _ig_count == 0:
                _tr_set_status("⚠️ Instagram returned 0 results — hashtag may be too specific.")
        elif "Instagram" in _tr_sources and not _tr_apify_key:
            st.warning("Instagram selected but APIFY_API_TOKEN is not set.")

        if "X/Twitter" in _tr_sources and _tr_apify_key:
            _tr_set_status(f"[X/Twitter] Searching '{_tr_topic}' via Apify…")
            for s in scrape_twitter(_tr_topic, api_token=_tr_apify_key,
                                    n=20, callback=_tr_cb):
                _tr_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "",
                                "thumbnail": (s.raw_meta or {}).get("thumbnail", "")})

        if "Exa" in _tr_sources and _tr_exa_key:
            for s in scrape_exa(_tr_topic, api_key=_tr_exa_key, n=15, callback=_tr_cb):
                _tr_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or ""})

    except Exception as _tr_src_err:
        st.warning(f"Some sources failed: {_tr_src_err}")

    # Deduplicate by title
    _tr_seen, _tr_deduped = set(), []
    for _r in _tr_raw:
        _k = _r.get("title","")[:60].lower()
        if _k not in _tr_seen:
            _tr_seen.add(_k)
            _tr_deduped.append(_r)

    # ── Round-robin balancing: interleave sources so no single source dominates ──
    from collections import defaultdict as _defdict
    _tr_src_buckets: dict = _defdict(list)
    for _r in _tr_deduped:
        _tr_src_buckets[_r.get("source", "other")].append(_r)
    _tr_balanced: list = []
    _tr_src_iters = {k: iter(v) for k, v in _tr_src_buckets.items()}
    while _tr_src_iters:
        for _src in list(_tr_src_iters):
            try:
                _tr_balanced.append(next(_tr_src_iters[_src]))
            except StopIteration:
                del _tr_src_iters[_src]
    _tr_raw = _tr_balanced[:90]

    # Build URL→thumbnail map — more reliable than asking Claude to reproduce URLs
    _tr_url_thumb: dict = {
        r.get("url", ""): r.get("thumbnail", "")
        for r in _tr_raw if r.get("url") and r.get("thumbnail")
    }

    # ── Haiku: sentiment + themes overview (fast, cheap batch classification) ──
    _tr_overview: dict = {}
    if _tr_raw and _tr_ant_key:
        try:
            import anthropic as _ant_ov
            _tr_set_status(f"Claude classifying sentiment and themes across {len(_tr_raw)} signals…")
            _ov_sig_txt = "\n".join(
                f"[{i}] {(s.get('content') or s.get('title',''))[:130]}"
                for i, s in enumerate(_tr_raw[:80])
            )
            _ov_prompt = f"""Topic / brand: "{_tr_topic}"

Analyse these {min(len(_tr_raw), 80)} social media signals and return a quantitative overview.

Return ONLY raw JSON, no explanation:
{{
  "sentiment": {{
    "positive": <integer count>,
    "negative": <integer count>,
    "neutral":  <integer count>,
    "summary":  "<10-12 words: dominant feeling and the main reason>"
  }},
  "themes": [
    {{
      "label":              "<2-4 word theme name>",
      "count":              <integer — how many signals mention this theme>,
      "dominant_sentiment": "positive" | "negative" | "neutral"
    }}
  ]
}}

Rules:
- Sentiment is relative to the topic/brand (is this signal positive/negative ABOUT it?)
- Identify 5 to 8 distinct themes that emerge from the signals (no fixed taxonomy)
- Sort themes by count descending
- Counts must add up reasonably (a signal can belong to 1-2 themes)

SIGNALS:
{_ov_sig_txt}"""
            _ov_resp = _ant_ov.Anthropic(api_key=_tr_ant_key).messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=900,
                messages=[{"role": "user", "content": _ov_prompt}],
            )
            _ov_txt = _ov_resp.content[0].text.strip()
            _ov_start = _ov_txt.find("{")
            _ov_end   = _ov_txt.rfind("}") + 1
            if _ov_start != -1 and _ov_end > _ov_start:
                _tr_overview = json.loads(_ov_txt[_ov_start:_ov_end])
        except Exception:
            _tr_overview = {}   # non-fatal — openings still render

    # Source volume (deterministic, no AI needed)
    _ov_src_counts: dict = {}
    for _r in _tr_raw:
        _ov_src_counts[_r.get("source", "other")] = _ov_src_counts.get(_r.get("source", "other"), 0) + 1

    _tr_overview["source_counts"] = _ov_src_counts

    # ── Claude: identify 3 Strategic Openings ────────────────────────────────
    _tr_set_status(f"Claude analysing {len(_tr_raw)} signals for strategic openings…")
    _tr_openings: list = []
    _tr_hunch_suggs: list = []

    if _tr_raw and _tr_ant_key:
        try:
            import anthropic as _ant_tr
            _tr_client = _ant_tr.Anthropic(api_key=_tr_ant_key)
            _tr_sig_txt = "\n".join(
                f"[{i}] {s['source'].upper()} | {s['title'][:100]}"
                f" | CONTENT:{s.get('content','')[:120]}"
                f" | URL:{s.get('url','')[:120]}"
                for i, s in enumerate(_tr_raw[:80])
            )
            _n_sigs = min(len(_tr_raw), 80)
            _tr_prompt = f"""You are a senior cultural strategist at an advertising agency.
Brand / topic: "{_tr_topic}"

You have {_n_sigs} social signals below. Your job is NOT to catalog or classify them.
Your job is to identify exactly 3 distinct, actionable creative opportunities — real tensions
or behaviours from these signals that a brand could specifically act on RIGHT NOW.

For each opportunity return:
- "tension": 3-6 word name capturing the cultural conflict or behaviour pattern (e.g. "Desk Lunch Shame", "Soup Status Anxiety", "Office Return Grief")
- "why_now": 2-3 sentences. Why is this happening? Why does it matter for THIS brand at THIS moment?
- "brand_angle": One concrete, specific creative direction the brand could take. Start with a verb.
- "hook": The content hook type that would work best. One of: "contrarian", "validation", "humor", "aspiration", "solidarity", "education", "challenge"
- "urgency": One of: "now" (act this week — conversation is peaking), "emerging" (act this month — building fast), "building" (watch closely — 4-8 week window)
- "signals": The 3-4 most relevant signal excerpts, each as:
  - "text": key quote or close paraphrase, max 25 words
  - "source": platform name (e.g. "reddit", "tiktok", "youtube")
  - "url": exact URL from the signals if available, else empty string

Also return:
- "suggested_hunches": 3 testable hypotheses derived from these signals. Each phrased as "People [verb]... because..." — max 18 words each.

IMPORTANT: Return ONLY raw JSON, no explanation. Format:
{{
  "openings": [
    {{
      "tension": "...", "why_now": "...", "brand_angle": "...",
      "hook": "...", "urgency": "now",
      "signals": [{{"text": "...", "source": "...", "url": "..."}}]
    }}
  ],
  "suggested_hunches": ["...", "...", "..."]
}}

SIGNALS:
{_tr_sig_txt}"""

            _tr_resp = _tr_client.messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
                max_tokens=3500,
                messages=[{"role": "user", "content": _tr_prompt}],
            )
            _tr_txt = _tr_resp.content[0].text.strip()
            # Strip markdown code fences
            _tr_txt = _re_global.sub(r'^```(?:json)?\s*', '', _tr_txt, flags=_re_global.MULTILINE)
            _tr_txt = _re_global.sub(r'\s*```\s*$', '', _tr_txt, flags=_re_global.MULTILINE)
            # Extract outermost JSON object
            _tr_js_start = _tr_txt.find("{")
            _tr_js_end   = _tr_txt.rfind("}") + 1
            if _tr_js_start != -1 and _tr_js_end > _tr_js_start:
                _tr_raw_json = _tr_txt[_tr_js_start:_tr_js_end]
                # Attempt 1 — direct parse
                try:
                    _tr_parsed = json.loads(_tr_raw_json)
                except json.JSONDecodeError:
                    # Attempt 2 — strip control chars and retry
                    _tr_raw_json = _re_global.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', _tr_raw_json)
                    _tr_parsed = json.loads(_tr_raw_json)
                _tr_openings    = _tr_parsed.get("openings", [])
                _tr_hunch_suggs = _tr_parsed.get("suggested_hunches", [])
            # Fallback: if Claude returned empty openings, build minimal cards from signals
            if not _tr_openings and _tr_raw:
                _tr_openings = [
                    {
                        "tension": _s["title"][:50],
                        "why_now": _s.get("content", "")[:180],
                        "brand_angle": "Explore this conversation further.",
                        "hook": "validation",
                        "urgency": "emerging",
                        "signals": [{"text": _s.get("content","")[:100],
                                     "source": _s.get("source",""), "url": _s.get("url","")}],
                    }
                    for _s in _tr_raw[:3]
                ]
        except Exception as _tr_cls_err:
            st.warning(f"Strategic analysis failed: {_tr_cls_err}")
            _tr_openings = []

    st.session_state["tr_openings"]          = _tr_openings
    st.session_state["tr_hunch_suggestions"] = _tr_hunch_suggs
    st.session_state["tr_overview"]          = _tr_overview
    st.session_state["tr_topic_used"]        = _tr_topic
    st.session_state["tr_terms_used"]        = _tr_expanded_terms
    st.session_state["tr_raw_count"]         = len(_tr_raw)
    st.session_state["tr_log"]               = _tr_log
    _tr_status.empty()

# ── Hunch: fetch & classify ───────────────────────────────────────────────────
if _tr_hunch_fetch and _tr_hunch.strip():
    _hn_apify_key   = os.environ.get("APIFY_API_TOKEN", "")
    _hn_youtube_key = os.environ.get("YOUTUBE_API_KEY", "")
    _hn_ant_key     = os.environ.get("ANTHROPIC_API_KEY", "")
    _hn_exa_key     = os.environ.get("EXA_API_KEY", "")
    _hn_raw: list[dict] = []
    _hn_status = st.empty()

    def _hn_set_status(msg: str) -> None:
        """Render a styled loading bar with animated dots (hunch section)."""
        _hn_status.markdown(
            f'<style>'
            f'@keyframes _lhblink{{0%,100%{{opacity:.15;}}50%{{opacity:1;}}}}'
            f'._lhd1{{display:inline-block;animation:_lhblink 1.2s 0s infinite;}}'
            f'._lhd2{{display:inline-block;animation:_lhblink 1.2s .4s infinite;}}'
            f'._lhd3{{display:inline-block;animation:_lhblink 1.2s .8s infinite;}}'
            f'</style>'
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:11.5px;'
            f'color:#0a5560;background:#e4f4f5;border-left:3px solid #0fa3b5;'
            f'border-radius:0 6px 6px 0;padding:7px 12px;margin:4px 0;">'
            f'{msg}'
            f'<span class="_lhd1">.</span><span class="_lhd2">.</span><span class="_lhd3">.</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # Use hunch text as the search topic
    _hn_topic = _tr_hunch.strip()

    try:
        from ingestion import (
            scrape_reddit, scrape_hacker_news, scrape_gdelt, scrape_rss,
            scrape_youtube, scrape_tiktok, scrape_instagram, scrape_twitter,
        )
        def _hn_cb(msg): _hn_set_status(msg)

        if "Saved signals" in _tr_sources:
            _hn_set_status("Searching saved signals…")
            _hn_all_db = load_signals(limit=500)
            _hn_words = set(_hn_topic.lower().split())
            for _s in _hn_all_db:
                _txt = f"{_s.get('title','')} {_s.get('content','')}".lower()
                if any(w in _txt for w in _hn_words if len(w) > 3):
                    _hn_raw.append({"title": _s.get("title",""),
                                    "content": _s.get("content","")[:300],
                                    "source": _s.get("source","rss"),
                                    "url": _s.get("url",""),
                                    "thumbnail": _s.get("thumbnail","")})
            _hn_raw = _hn_raw[:50]

        if "Reddit" in _tr_sources:
            _hn_set_status(f"[Reddit] Searching '{_hn_topic[:30]}'…")
            for s in scrape_reddit(_hn_topic, max_items=20, callback=_hn_cb):
                _hn_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "", "thumbnail": ""})

        if "Hacker News" in _tr_sources:
            _hn_set_status(f"[Hacker News] Searching '{_hn_topic[:30]}'…")
            for s in scrape_hacker_news(_hn_topic, n=15, callback=_hn_cb):
                _hn_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "", "thumbnail": ""})

        if "YouTube" in _tr_sources and _hn_youtube_key:
            # Search each keyword individually — multi-word phrases return 0 on YouTube API
            _hn_yt_kws = [w for w in _re_global.sub(r"[^\w\s]","",_hn_topic.lower()).split()
                          if len(w) > 3][:3] or [_hn_topic[:30]]
            _hn_yt_seen: set = set()
            for _hn_yt_kw in _hn_yt_kws:
                _hn_set_status(f"[YouTube] Searching '{_hn_yt_kw}' (GB)…")
                for s in scrape_youtube(_hn_yt_kw, api_key=_hn_youtube_key,
                                        n=5, region_code="GB", callback=_hn_cb):
                    if s.url not in _hn_yt_seen:
                        _hn_yt_seen.add(s.url)
                        _hn_raw.append({"title": s.title, "content": s.content[:300],
                                        "source": s.source, "url": s.url or "",
                                        "thumbnail": (s.raw_meta or {}).get("thumbnail","")})

        if "TikTok" in _tr_sources and _hn_apify_key:
            _hn_set_status(f"[TikTok] Searching '{_hn_topic[:30]}' via Apify…")
            for s in scrape_tiktok(_hn_topic, api_token=_hn_apify_key,
                                   n=15, fetch_comments=False, callback=_hn_cb):
                _hn_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "",
                                "thumbnail": (s.raw_meta or {}).get("thumbnail","")})

        if "Instagram" in _tr_sources and _hn_apify_key:
            _hn_set_status(f"[Instagram] Searching '{_hn_topic[:30]}' via Apify…")
            for s in scrape_instagram(_hn_topic, api_token=_hn_apify_key,
                                      n=15, callback=_hn_cb):
                _hn_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "",
                                "thumbnail": (s.raw_meta or {}).get("thumbnail","")})

        if "X/Twitter" in _tr_sources and _hn_apify_key:
            _hn_set_status(f"[X/Twitter] Searching '{_hn_topic[:30]}' via Apify…")
            for s in scrape_twitter(_hn_topic, api_token=_hn_apify_key,
                                    n=15, callback=_hn_cb):
                _hn_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "", "thumbnail": ""})

        if "RSS" in _tr_sources:
            _hn_set_status("Reading RSS feeds…")
            for s in scrape_rss(max_items_per_feed=4, callback=_hn_cb):
                _hn_raw.append({"title": s.title, "content": s.content[:300],
                                "source": s.source, "url": s.url or "", "thumbnail": ""})

    except Exception as _hn_src_err:
        st.warning(f"Some sources failed: {_hn_src_err}")

    # Deduplicate
    _hn_seen, _hn_deduped = set(), []
    for _r in _hn_raw:
        _k = _r.get("title","")[:60].lower()
        if _k not in _hn_seen:
            _hn_seen.add(_k)
            _hn_deduped.append(_r)
    _hn_raw = _hn_deduped[:70]

    # Claude classifies each finding vs. the hunch
    _hn_board = {"confirms": [], "challenges": [], "unexpected": []}
    if _hn_raw and _hn_ant_key:
        _hn_set_status(f"Claude analysing {len(_hn_raw)} signals against your hunch…")
        try:
            import anthropic as _ant_hn
            _hn_sig_txt = "\n".join(
                f"[{i}] {s['source'].upper()} | {s['title'][:100]} | {s['content'][:200]}"
                f" | URL:{s.get('url','')[:80]}"
                f"{' | THUMB:' + s.get('thumbnail','')[:300] if s.get('thumbnail') else ''}"
                for i, s in enumerate(_hn_raw[:60])
            )
            _hn_prompt = f"""You are a cultural intelligence analyst testing a hypothesis.

HUNCH: "{_hn_topic}"

Analyse these {min(len(_hn_raw),60)} signals and identify 8–12 relevant findings.

For each finding return:
- "name": 2–5 words, title case — the finding's headline
- "relation": one of "CONFIRMS", "CHALLENGES", or "UNEXPECTED"
  * CONFIRMS = evidence that supports or validates the hunch
  * CHALLENGES = evidence that contradicts or complicates the hunch
  * UNEXPECTED = interesting finding that emerged but wasn't in the hypothesis
- "quote": the most relevant 1–2 sentences from a signal that best illustrates this (exact excerpt or close paraphrase)
- "source": the source name (e.g. "reddit", "youtube")
- "note": one sentence max 12 words explaining why this matters strategically
- "urls": up to 2 URLs from the signals (exact URLs starting with http)
- "thumbnail": a THUMB: value from the signals if available, else empty string

Prioritise signals most relevant to the hunch. Skip off-topic noise.
Respond ONLY with a valid JSON array.

SIGNALS:
{_hn_sig_txt}"""

            _hn_resp = _ant_hn.Anthropic(api_key=_hn_ant_key).messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
                max_tokens=2500,
                messages=[{"role": "user", "content": _hn_prompt}],
            )
            _hn_txt = _hn_resp.content[0].text.strip()

            # ── Robust JSON extraction ────────────────────────────────────────
            # Strip markdown code fences Claude sometimes adds
            _hn_txt = _re_global.sub(r'^```(?:json)?\s*', '', _hn_txt, flags=_re_global.MULTILINE)
            _hn_txt = _re_global.sub(r'\s*```\s*$', '', _hn_txt, flags=_re_global.MULTILINE)

            _hn_a0 = _hn_txt.find("[")
            _hn_a1 = _hn_txt.rfind("]")
            _hn_js = _hn_txt[_hn_a0:_hn_a1 + 1] if _hn_a0 != -1 else "[]"

            _hn_findings: list = []

            # Attempt 1 — direct parse
            try:
                _hn_findings = json.loads(_hn_js)
            except json.JSONDecodeError:
                # Attempt 2 — strip ASCII control chars (except \t \n \r) and retry
                _hn_js2 = _re_global.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', _hn_js)
                try:
                    _hn_findings = json.loads(_hn_js2)
                except json.JSONDecodeError:
                    # Attempt 3 — extract individual objects with regex (graceful degradation)
                    for _mo in _re_global.finditer(r'\{[^{}]*\}', _hn_js2, _re_global.DOTALL):
                        try:
                            _hn_findings.append(json.loads(_mo.group()))
                        except json.JSONDecodeError:
                            pass

            for _fnd in _hn_findings:
                _rel = _fnd.get("relation","UNEXPECTED").upper()
                _hcard = {
                    "name":      _fnd.get("name",""),
                    "relation":  _rel,
                    "quote":     _fnd.get("quote",""),
                    "source":    _fnd.get("source",""),
                    "note":      _fnd.get("note",""),
                    "urls":      [u for u in _fnd.get("urls",[]) if u.startswith("http")][:2],
                    "thumbnail": _fnd.get("thumbnail","") if str(_fnd.get("thumbnail","")).startswith("http") else "",
                }
                if _rel == "CONFIRMS":
                    _hn_board["confirms"].append(_hcard)
                elif _rel == "CHALLENGES":
                    _hn_board["challenges"].append(_hcard)
                else:
                    _hn_board["unexpected"].append(_hcard)
        except Exception as _hn_cls_err:
            st.warning(f"Hunch analysis failed: {_hn_cls_err}")

    st.session_state["tr_hunch_board"] = _hn_board
    st.session_state["tr_hunch_text"]  = _hn_topic
    _hn_status.empty()

# ── Render board ──────────────────────────────────────────────────────────────
# ── Auto-load: Source Gallery (instant, no Claude needed) ─────────────────────
if "tr_gallery" not in st.session_state:
    _tr_auto_raw_sigs = load_signals(limit=400)
    _gal_order_pref = ["youtube", "tiktok", "instagram", "twitter",
                       "reddit", "google_trends", "hacker_news", "rss", "exa", "gdelt"]
    _gal_map: dict = {}
    for _gs in _tr_auto_raw_sigs:
        _gal_map.setdefault(_gs.get("source", "other"), []).append(_gs)
    _gal_ordered = [s for s in _gal_order_pref if s in _gal_map]
    _gal_ordered += sorted([s for s in _gal_map if s not in _gal_ordered])
    st.session_state["tr_gallery"] = {
        "ordered": _gal_ordered,
        "by_source": _gal_map,
        "total": len(_tr_auto_raw_sigs),
    }

_tr_openings_data = st.session_state.get("tr_openings", None)   # new Strategic Openings
_tr_board_data    = st.session_state.get("tr_board", None)       # legacy kanban (kept for compat)
_tr_gallery_data  = st.session_state.get("tr_gallery", None)
_tr_topic_label   = st.session_state.get("tr_topic_used", "")
_tr_terms_label   = st.session_state.get("tr_terms_used", [])
_tr_any_result    = _tr_openings_data is not None or _tr_board_data is not None

def _tr_thumb_from_url(url: str) -> str:
    """Derive a thumbnail URL from a content URL where possible."""
    if not url:
        return ""
    # YouTube
    _yt = _re_global.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    if _yt:
        return f"https://i.ytimg.com/vi/{_yt.group(1)}/mqdefault.jpg"
    return ""

_tr_src_cls = {
    "reddit": "tr-src-reddit", "google_trends": "tr-src-google_trends",
    "hacker_news": "tr-src-hacker_news", "youtube": "tr-src-youtube",
    "tiktok": "tr-src-tiktok", "instagram": "tr-src-instagram",
    "twitter": "tr-src-twitter", "gdelt": "tr-src-gdelt",
    "rss": "tr-src-rss", "exa": "tr-src-exa",
}
_tr_vel_cls  = {"RISING": "tr-vel-high", "STABLE": "tr-vel-stable", "DECLINING": "tr-vel-decline"}
_tr_vel_icon = {"RISING": "▲", "STABLE": "→", "DECLINING": "▼"}

def _tr_render_card(card: dict, col_key: str, idx: int):
    srcs = card.get("sources", [])
    src_badges = " ".join(
        f'<span class="tr-src {_tr_src_cls.get(s,"tr-src-rss")}">{e(s.replace("_"," "))}</span>'
        for s in srcs[:3]
    )
    vel       = card.get("velocity", "STABLE")
    vel_cls   = _tr_vel_cls.get(vel, "tr-vel-stable")
    vel_icon  = _tr_vel_icon.get(vel, "➡️")
    score_lbl = card.get("score_label", "")
    urls      = card.get("urls", [])

    # Derive best thumbnail: from card's stored thumb, or from URL
    thumb = card.get("thumbnail", "")
    if not thumb:
        for _u in urls:
            thumb = _tr_thumb_from_url(_u)
            if thumb:
                break

    thumb_src = _tr_proxy_thumb(thumb)

    # Placeholder class for sources whose CDN blocks hotlinks
    _card_source = (srcs[0] if srcs else "").lower()
    _ph_cls = {"tiktok": "tr-ph-tiktok", "instagram": "tr-ph-instagram"}.get(_card_source, "")

    if thumb_src:
        # Overlay img on top of placeholder bg; if img fails, hide it (gradient shows)
        thumb_html = (
            f'<div class="tr-thumb-wrap {_ph_cls}">'
            f'<img src="{thumb_src}" style="opacity:0;transition:opacity .4s;" '
            f'onload="this.style.opacity=1" onerror="this.style.display=\'none\'" />'
            f'</div>'
        )
    elif _ph_cls:
        # No URL at all — show branded placeholder
        thumb_html = f'<div class="tr-thumb-wrap {_ph_cls}"></div>'
    else:
        thumb_html = ""

    # Link row
    link_html = ""
    if urls:
        links = " · ".join(
            f'<a href="{u}" target="_blank" style="color:#6ea8c4;font-size:10px;'
            f'text-decoration:none;">{urllib.parse.urlparse(u).netloc or u[:30]}</a>'
            for u in urls
        )
        link_html = f'<div style="margin-top:6px;">{links}</div>'

    # Dimension tags
    _dims_html = ""
    _dim_parts = []
    if card.get("emotion"):
        _dim_parts.append(f'<span class="tr-dim tr-dim-emotion">● {e(card["emotion"])}</span>')
    if card.get("hook"):
        _dim_parts.append(f'<span class="tr-dim tr-dim-hook">⚡ {e(card["hook"])}</span>')
    if card.get("tone"):
        _dim_parts.append(f'<span class="tr-dim tr-dim-tone">~ {e(card["tone"])}</span>')
    if _dim_parts:
        _dims_html = f'<div class="tr-dims">{"".join(_dim_parts)}</div>'

    st.markdown(f"""
<div class="tr-card">
  {thumb_html}
  <div class="tr-card-top">
    {src_badges}
    <span class="tr-vel {vel_cls}">{vel_icon} {e(score_lbl)}</span>
  </div>
  <div class="tr-card-name">{e(card.get("name",""))}</div>
  {"<div class='tr-card-note'>" + e(card.get("note","")) + "</div>" if card.get("note") else ""}
  {_dims_html}
  {link_html}
</div>""", unsafe_allow_html=True)

    # Action row: label + 3 buttons (Pin, move A, move B) in one aligned row
    st.markdown('<div style="font-size:10px;color:#9dc4d8;margin:8px 0 4px;'
                'letter-spacing:.05em;text-transform:uppercase;">Move to board →</div>',
                unsafe_allow_html=True)
    _dests = [d for d in [("high","↑ Rising"),("stable","→ Stable"),("decline","↓ Cooling")]
              if d[0] != col_key]
    _bc_pin, _bc1, _bc2 = st.columns(3)
    with _bc_pin:
        if st.button("📌 Pin", key=f"tr_pin_{col_key}_{idx}", use_container_width=True,
                     help="Save this theme to your project"):
            _tr_folders_pin = load_project_folders()
            if _tr_folders_pin:
                _pin_user = st.session_state.get("logged_in_user", "internal")
                _vel_icon_p = _tr_vel_icon.get(vel, "")
                add_curadoria_item(
                    _pin_user, "trend",
                    f"{_vel_icon_p} {card.get('name','')} {score_lbl}",
                    f"{card.get('note','')}\n\nSources: {', '.join(srcs)}"
                    + (f"\n{urls[0]}" if urls else ""),
                )
                st.success("Pinned!")
            else:
                st.warning("Create a project folder first.")
    for _bi, (_dk, _dl) in enumerate(_dests):
        with (_bc1 if _bi == 0 else _bc2):
            if st.button(_dl, key=f"tr_mv_{col_key}_{idx}_{_dk}",
                         use_container_width=True):
                _brd = st.session_state.get("tr_board", {})
                _card_obj = _brd.get(col_key, [])[idx]
                _brd[col_key].pop(idx)
                _brd.setdefault(_dk, []).append(_card_obj)
                st.session_state["tr_board"] = _brd
                st.rerun()

# Source Gallery removed — replaced by Hunch-first search flow

# ── Read overview from session state ──────────────────────────────────────────
_tr_overview_data = st.session_state.get("tr_overview", {})

# Strategic Openings render block moved to after Hunch board (below)

_hn_board_shown = bool(st.session_state.get("tr_hunch_board"))
if not _tr_any_result and not _hn_board_shown:
    st.markdown("""
<div style="text-align:center;padding:3rem 2rem;color:#9dc4d8;">
  <div style="font-size:1.8rem;margin-bottom:0.8rem;">💡</div>
  <div style="font-size:14px;font-family:Georgia,serif;">
    Type a hypothesis above and click <em>Find Evidence</em>
  </div>
  <div style="font-size:11px;margin-top:6px;font-family:monospace;
    letter-spacing:.06em;text-transform:uppercase;">
    Claude maps what confirms, challenges, and surprises — with a heat map
  </div>
</div>""", unsafe_allow_html=True)

# ── Hunch board render ────────────────────────────────────────────────────────
_hn_board_data = st.session_state.get("tr_hunch_board", None)
_hn_text_used  = st.session_state.get("tr_hunch_text", "")

if _hn_board_data:
    _hn_total = sum(len(v) for v in _hn_board_data.values())
    st.markdown(f"""
<div style="padding:.6rem 0 .8rem;">
  <div style="font-family:monospace;font-size:10px;letter-spacing:.18em;
    text-transform:uppercase;color:#7c3aed;margin-bottom:4px;">Evidence Map</div>
  <div style="font-size:1.1rem;font-family:Georgia,serif;color:#071828;margin-bottom:2px;">
    "{e(_hn_text_used)}"
  </div>
  <div style="font-size:11px;color:#9dc4d8;margin-top:3px;">{_hn_total} findings mapped</div>
</div>""", unsafe_allow_html=True)

    # ── Heat Map ─────────────────────────────────────────────────────────────
    _hm_confirms   = _hn_board_data.get("confirms", [])
    _hm_unexpected = _hn_board_data.get("unexpected", [])
    _hm_challenges = _hn_board_data.get("challenges", [])

    # Tile class: 3 intensities per category (first = strongest)
    _hm_hot_cls  = ["hm-hot-1",  "hm-hot-2",  "hm-hot-3"]
    _hm_mid_cls  = ["hm-mid-1",  "hm-mid-2",  "hm-mid-3"]
    _hm_cold_cls = ["hm-cold-1", "hm-cold-2", "hm-cold-3"]

    def _hm_tile(finding, cat_label, icon, cls_list, idx):
        cls = cls_list[min(idx, len(cls_list) - 1)]
        return (
            f'<div class="hm-tile {cls}">'
            f'<div class="hm-tile-cat">{icon} {cat_label}</div>'
            f'<div class="hm-tile-name">{e(finding.get("name","")[:42])}</div>'
            f'</div>'
        )

    _hm_tiles = ""
    for _i, _f in enumerate(_hm_confirms):
        _hm_tiles += _hm_tile(_f, "Confirms", "✓", _hm_hot_cls, _i)
    for _i, _f in enumerate(_hm_unexpected):
        _hm_tiles += _hm_tile(_f, "Unexpected", "◎", _hm_mid_cls, _i)
    for _i, _f in enumerate(_hm_challenges):
        _hm_tiles += _hm_tile(_f, "Challenges", "✗", _hm_cold_cls, _i)

    st.markdown(f"""
<div class="hm-wrap">
  <div class="hm-legend">
    <span class="hm-legend-label">cold</span>
    <div class="hm-legend-bar"></div>
    <span class="hm-legend-label">hot</span>
  </div>
  <div class="hm-grid">{_hm_tiles}</div>
</div>""", unsafe_allow_html=True)
    st.markdown("---")

    def _hn_render_card(card: dict, col_key: str, idx: int):
        src = card.get("source","")
        src_cls = _tr_src_cls.get(src, "tr-src-rss")
        rel = card.get("relation","UNEXPECTED")
        rel_cls = {"CONFIRMS":"hn-rel-confirms","CHALLENGES":"hn-rel-challenges"}.get(rel,"hn-rel-unexpected")
        rel_icon = {"CONFIRMS":"✓","CHALLENGES":"✗","UNEXPECTED":"◎"}.get(rel,"◎")
        urls = card.get("urls",[])
        thumb = card.get("thumbnail","")
        thumb_src = _tr_proxy_thumb(thumb)
        _hn_src = card.get("source","").lower()
        _hn_ph_cls = {"tiktok": "tr-ph-tiktok", "instagram": "tr-ph-instagram"}.get(_hn_src, "")
        if thumb_src:
            thumb_html = (
                f'<div class="tr-thumb-wrap {_hn_ph_cls}" style="height:100px;">'
                f'<img src="{thumb_src}" style="opacity:0;transition:opacity .4s;" '
                f'onload="this.style.opacity=1" onerror="this.style.display=\'none\'" />'
                f'</div>'
            )
        elif _hn_ph_cls:
            thumb_html = f'<div class="tr-thumb-wrap {_hn_ph_cls}" style="height:100px;"></div>'
        else:
            thumb_html = ""
        link_html = ""
        if urls:
            links = " · ".join(
                f'<a href="{u}" target="_blank" style="color:#6ea8c4;font-size:10px;'
                f'text-decoration:none;">{urllib.parse.urlparse(u).netloc or u[:30]}</a>'
                for u in urls
            )
            link_html = f'<div style="margin-top:6px;">{links}</div>'
        quote_html = (
            f'<div class="hn-card-quote">{e(card.get("quote",""))}</div>'
            if card.get("quote") else ""
        )
        st.markdown(f"""
<div class="hn-card">
  {thumb_html}
  <div class="hn-card-top">
    <span class="tr-src {src_cls}">{e(src.replace("_"," "))}</span>
    <span class="hn-rel {rel_cls}">{rel_icon} {rel.title()}</span>
  </div>
  <div class="hn-card-name">{e(card.get("name",""))}</div>
  {quote_html}
  {"<div class='hn-card-note'>" + e(card.get("note","")) + "</div>" if card.get("note") else ""}
  {link_html}
</div>""", unsafe_allow_html=True)
        # Pin button
        if st.button("📌 Pin", key=f"hn_pin_{col_key}_{idx}", use_container_width=True,
                     help="Save this finding to your project"):
            _hn_folders_pin = load_project_folders()
            if _hn_folders_pin:
                _hn_pin_user = st.session_state.get("logged_in_user","internal")
                add_curadoria_item(
                    _hn_pin_user, "hunch_finding",
                    f"{rel_icon} {card.get('name','')} [{rel.title()}]",
                    f"Hunch: {_hn_text_used}\n\n{card.get('quote','')}\n\n{card.get('note','')}"
                    + (f"\n{urls[0]}" if urls else ""),
                )
                st.success("Pinned!")
            else:
                st.warning("Create a project folder first.")

    _hn_c1, _hn_c2, _hn_c3 = st.columns(3)
    with _hn_c1:
        _hc = len(_hn_board_data.get("confirms",[]))
        st.markdown(f"""
<div class="hn-col-wrap hn-col-confirms">
  <div class="hn-col-header">✓ Confirms <span style="font-weight:400;opacity:.55;margin-left:4px;">({_hc})</span></div>
</div>""", unsafe_allow_html=True)
        for _i, _c in enumerate(_hn_board_data.get("confirms",[])):
            _hn_render_card(_c, "confirms", _i)

    with _hn_c2:
        _hch = len(_hn_board_data.get("challenges",[]))
        st.markdown(f"""
<div class="hn-col-wrap hn-col-challenges">
  <div class="hn-col-header">✗ Challenges <span style="font-weight:400;opacity:.55;margin-left:4px;">({_hch})</span></div>
</div>""", unsafe_allow_html=True)
        for _i, _c in enumerate(_hn_board_data.get("challenges",[])):
            _hn_render_card(_c, "challenges", _i)

    with _hn_c3:
        _hu = len(_hn_board_data.get("unexpected",[]))
        st.markdown(f"""
<div class="hn-col-wrap hn-col-unexpected">
  <div class="hn-col-header">◎ Unexpected <span style="font-weight:400;opacity:.55;margin-left:4px;">({_hu})</span></div>
</div>""", unsafe_allow_html=True)
        for _i, _c in enumerate(_hn_board_data.get("unexpected",[])):
            _hn_render_card(_c, "unexpected", _i)

    # Save hunch to project
    st.markdown("---")
    _hn_sv1, _hn_sv2 = st.columns([3,1])
    with _hn_sv1:
        _hn_folders = load_project_folders()
        _hn_folder_opts = {f.get("name","?"): f.get("id","") for f in _hn_folders}
        _hn_sel_folder = st.selectbox(
            "Save hunch to project", options=list(_hn_folder_opts.keys()),
            key="hn_save_folder", label_visibility="collapsed",
        ) if _hn_folder_opts else None
    with _hn_sv2:
        if st.button("Save Hunch →", key="hn_save", use_container_width=True, type="primary"):
            if _hn_sel_folder and _hn_folder_opts:
                _hn_md = f'**Hunch: “{_hn_text_used}”**\n\n'
                for _col_name, _col_icon in [("confirms","✓ Confirms"),
                                              ("challenges","✗ Challenges"),
                                              ("unexpected","◎ Unexpected")]:
                    _hn_md += f"\n### {_col_icon}\n"
                    for _c in _hn_board_data.get(_col_name,[]):
                        _url_str = (" — " + _c["urls"][0]) if _c.get("urls") else ""
                        _hn_md += f"- **{_c['name']}** — {_c.get('note','')}{_url_str}\n"
                        if _c.get("quote"):
                            _hn_md += f"  > {_c['quote']}\n"
                add_curadoria_item(
                    st.session_state.get("logged_in_user","internal"),
                    "hunch_board",
                    f"Hunch: {_hn_text_used}",
                    _hn_md,
                )
                st.success(f"Hunch saved to **{_hn_sel_folder}**!")
            else:
                st.warning("No project folders yet — create one in Projects first.")

# ── Strategic Openings — rendered after Hunch board ───────────────────────────
if _tr_openings_data is not None:
    _tr_raw_count = st.session_state.get("tr_raw_count", None)
    st.markdown("---")
    if not _tr_openings_data:
        if _tr_raw_count == 0:
            st.warning(
                "**No signals returned from selected sources.** "
                "Possible causes: API rate limits reached, no posts found for this topic/hashtag, "
                "or APIFY_API_TOKEN may be missing. Try adding RSS, Reddit, or YouTube as extra sources."
            )
            _tr_log_show = st.session_state.get("tr_log", [])
            if _tr_log_show:
                with st.expander("🔍 Debug log — what happened during the search", expanded=True):
                    for _msg in _tr_log_show:
                        st.caption(_msg)
        else:
            st.info("No openings found — try a broader topic or add more sources.")
    else:
        _terms_str = ", ".join(_tr_terms_label[:4]) if _tr_terms_label else _tr_topic_label
        st.markdown(f"""
<div style="padding:.4rem 0 1rem;">
  <div style="font-family:monospace;font-size:10px;letter-spacing:.18em;
    text-transform:uppercase;color:#0a7d8c;margin-bottom:4px;">Strategic Openings</div>
  <div style="font-family:Georgia,serif;font-size:1.05rem;color:#071828;margin-bottom:2px;">
    What can this brand do with these signals — right now?
  </div>
  <div style="font-size:11px;color:#9dc4d8;">{len(_tr_openings_data)} openings · {_tr_raw_count or 0} signals · topic: {e(_terms_str)}</div>
</div>""", unsafe_allow_html=True)

        # ── Overview: sentiment + themes + sources ────────────────────────────
        if _tr_overview_data:
            _ov_sent   = _tr_overview_data.get("sentiment", {})
            _ov_themes = _tr_overview_data.get("themes", [])
            _ov_srcs   = _tr_overview_data.get("source_counts", {})
            _ov_pos = int(_ov_sent.get("positive", 0))
            _ov_neg = int(_ov_sent.get("negative", 0))
            _ov_neu = int(_ov_sent.get("neutral",  0))
            _ov_total_sent = max(_ov_pos + _ov_neg + _ov_neu, 1)
            _ov_pos_pct = round(_ov_pos / _ov_total_sent * 100)
            _ov_neg_pct = round(_ov_neg / _ov_total_sent * 100)
            _ov_neu_pct = 100 - _ov_pos_pct - _ov_neg_pct
            _ov_bar_segs = ""
            if _ov_pos_pct:
                _ov_bar_segs += f'<div class="ov-sent-seg ov-sent-pos" style="width:{_ov_pos_pct}%">{_ov_pos_pct}%</div>'
            if _ov_neg_pct:
                _ov_bar_segs += f'<div class="ov-sent-seg ov-sent-neg" style="width:{_ov_neg_pct}%">{_ov_neg_pct}%</div>'
            if _ov_neu_pct:
                _ov_bar_segs += f'<div class="ov-sent-seg ov-sent-neu" style="width:{_ov_neu_pct}%">{_ov_neu_pct}%</div>'
            _ov_theme_cls = {"positive": "ov-theme-pos", "negative": "ov-theme-neg", "neutral": "ov-theme-neu"}
            _ov_themes_html = "".join(
                f'<div class="ov-theme-chip {_ov_theme_cls.get(_th.get("dominant_sentiment","neutral"),"ov-theme-neu")}">'
                f'{e(_th.get("label",""))}<span class="ov-theme-count">{_th.get("count","")}</span></div>'
                for _th in _ov_themes[:8]
            )
            _ov_src_html = "".join(
                f'<span class="ov-src-pill tr-src {_tr_src_cls.get(_src,"tr-src-rss")}">'
                f'{e(_src.replace("_"," ").title())} · {_cnt}</span> '
                for _src, _cnt in sorted(_ov_srcs.items(), key=lambda x: -x[1])
            )
            _ov_summary = e(_ov_sent.get("summary", ""))
            st.markdown(f"""
<div class="ov-wrap">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
    <div>
      <div class="ov-section-label">Sentiment about "{e(_tr_topic_label)}"</div>
      <div class="ov-sent-bar">{_ov_bar_segs}</div>
      <div class="ov-sent-legend">
        <div class="ov-sent-leg-item"><div class="ov-sent-dot" style="background:#16a34a"></div>Positive {_ov_pos_pct}%</div>
        <div class="ov-sent-leg-item"><div class="ov-sent-dot" style="background:#dc2626"></div>Negative {_ov_neg_pct}%</div>
        <div class="ov-sent-leg-item"><div class="ov-sent-dot" style="background:#94a3b8"></div>Neutral {_ov_neu_pct}%</div>
      </div>
      {"<div class='ov-sent-summary'>" + _ov_summary + "</div>" if _ov_summary else ""}
    </div>
    <div>
      <div class="ov-section-label">Sources ({_tr_raw_count or 0} signals)</div>
      <div class="ov-sources">{_ov_src_html}</div>
    </div>
  </div>
  {"<div style='margin-top:16px;border-top:1px solid #f0f4f8;padding-top:14px;'><div class='ov-section-label'>Top themes</div><div class='ov-themes'>" + _ov_themes_html + "</div></div>" if _ov_themes else ""}
</div>""", unsafe_allow_html=True)

        # ── Opening cards ─────────────────────────────────────────────────────
        _so_urgency_cfg = {
            "now":      ("so-urgency-now",      "● Act now"),
            "emerging": ("so-urgency-emerging",  "◑ Emerging"),
            "building": ("so-urgency-building",  "○ Building"),
        }
        _so_hook_icons = {
            "contrarian": "⚡", "validation": "✓", "humor": "😄",
            "aspiration": "✦", "solidarity": "🤝", "education": "◎", "challenge": "→",
        }
        for _oi, _op in enumerate(_tr_openings_data):
            _urgency_key = (_op.get("urgency") or "emerging").lower()
            _urg_cls, _urg_lbl = _so_urgency_cfg.get(_urgency_key, ("so-urgency-emerging", "◑ Emerging"))
            _hook = _op.get("hook", "")
            _hook_icon = _so_hook_icons.get(_hook.lower(), "→")
            _sig_html = ""
            for _sig in (_op.get("signals") or [])[:4]:
                _sig_url  = _sig.get("url", "")
                _src_cls  = _tr_src_cls.get(_sig.get("source",""), "tr-src-rss")
                _link_part = (
                    f' <a href="{e(_sig_url)}" target="_blank" '
                    f'style="color:#9dc4d8;font-size:10px;text-decoration:none;">→ source</a>'
                ) if _sig_url else ""
                _sig_html += (
                    f'<div class="so-signal">'
                    f'<div class="so-signal-quote">"{e(_sig.get("text",""))}"</div>'
                    f'<div class="so-signal-meta">'
                    f'<span class="tr-src {_src_cls}">{e(_sig.get("source","").replace("_"," "))}</span>'
                    f'{_link_part}</div></div>'
                )
            st.markdown(f"""
<div class="so-card so-card-{_urgency_key}">
  <div class="so-header">
    <div class="so-tension">{e(_op.get("tension",""))}</div>
    <div class="so-urgency {_urg_cls}">{_urg_lbl}</div>
  </div>
  <div class="so-why-now">{e(_op.get("why_now",""))}</div>
  <div class="so-signals-label">Evidence from signals</div>
  {_sig_html}
  <div class="so-angle-wrap">
    <div class="so-angle-label">Brand angle</div>
    <div class="so-angle-text">{e(_op.get("brand_angle",""))}</div>
    {'<div class="so-hook">' + _hook_icon + ' ' + e(_hook) + '</div>' if _hook else ''}
  </div>
</div>""", unsafe_allow_html=True)
            _pin_col2, _brief_col2, _sp2 = st.columns([1, 2, 3])
            with _pin_col2:
                if st.button("📌 Pin", key=f"so_pin2_{_oi}", use_container_width=True):
                    _pu = st.session_state.get("logged_in_user", "internal")
                    add_curadoria_item(_pu, "strategic_opening",
                                       f"Opening: {_op.get('tension','')}",
                                       f"**Why now:** {_op.get('why_now','')}\n\n**Angle:** {_op.get('brand_angle','')}")
                    st.success("Saved!")
            with _brief_col2:
                if st.button("→ Build Brief", key=f"so_brief2_{_oi}",
                             use_container_width=True, type="primary"):
                    _bu = st.session_state.get("logged_in_user", "internal")
                    _tb = _op.get("tension", "")
                    _ab = _op.get("brand_angle", "")
                    add_curadoria_item(_bu, "strategic_opening", f"Opening: {_tb}",
                                       f"**Why now:** {_op.get('why_now','')}\n\n**Angle:** {_ab}")
                    st.session_state["briefing_prefill"] = f"{_tb} — {_ab}"
                    st.success("Saved! Go to **Dispatches → Briefing Builder**.")
            st.markdown("<hr style='border:none;border-top:1px solid #f0f4f8;margin:0 0 4px;'>",
                        unsafe_allow_html=True)

# ── Signal Intelligence Map — visual heat map at bottom of Trends tab ─────────
_viz_board    = st.session_state.get("tr_hunch_board")
_viz_overview = st.session_state.get("tr_overview", {})
_viz_hunch_text = st.session_state.get("tr_hunch_text", "")
_viz_topic_text = st.session_state.get("tr_topic_used", "")

if _viz_board or _viz_overview.get("themes"):
    st.markdown("---")
    st.markdown("""
<div style="padding:.4rem 0 .6rem;">
  <div style="font-family:monospace;font-size:10px;letter-spacing:.18em;
    text-transform:uppercase;color:#0a7d8c;margin-bottom:4px;">Signal Intelligence Map</div>
  <div style="font-family:Georgia,serif;font-size:1.05rem;color:#071828;">
    Evidence distribution across platforms &amp; themes
  </div>
</div>""", unsafe_allow_html=True)

    try:
        import plotly.graph_objects as go

        # ── Chart 1: Source × Category heat map (from hunch board) ────────────
        if _viz_board:
            _col_keys  = ["confirms", "challenges", "unexpected"]
            _col_names = ["✓ Confirms", "✗ Challenges", "◎ Unexpected"]

            # Collect sources with at least 1 finding
            _src_count: dict[str, list] = {}
            for _ci, _ck in enumerate(_col_keys):
                for _card in _viz_board.get(_ck, []):
                    _src = _card.get("source", "other").replace("_", " ").title()
                    if _src not in _src_count:
                        _src_count[_src] = [0, 0, 0]
                    _src_count[_src][_ci] += 1

            # Sort sources by total signal count desc
            _src_sorted = sorted(_src_count.keys(),
                                 key=lambda s: sum(_src_count[s]), reverse=True)
            _z = [_src_count[s] for s in _src_sorted]
            _z_text = [[str(v) if v > 0 else "" for v in row] for row in _z]

            _hm_fig = go.Figure(data=go.Heatmap(
                z=_z,
                x=_col_names,
                y=_src_sorted,
                colorscale=[
                    [0.0,  "#0d2535"],
                    [0.25, "#0a4a5c"],
                    [0.55, "#0a7d8c"],
                    [0.78, "#e07b20"],
                    [1.0,  "#f59e0b"],
                ],
                showscale=True,
                text=_z_text,
                texttemplate="%{text}",
                textfont={"size": 15, "color": "white", "family": "Georgia,serif"},
                hoverongaps=False,
                xgap=4,
                ygap=4,
                colorbar=dict(
                    thickness=12,
                    tickfont=dict(color="#6ea8c4", size=10, family="monospace"),
                    title=dict(text="signals", font=dict(color="#6ea8c4", size=10,
                                                          family="monospace"), side="right"),
                    outlinewidth=0,
                ),
            ))
            _hm_title = f'"{_viz_hunch_text[:55]}…"' if len(_viz_hunch_text) > 55 else f'"{_viz_hunch_text}"'
            _hm_fig.update_layout(
                title=dict(
                    text=f"Evidence map — hunch: {_hm_title}",
                    font=dict(size=11, color="#6ea8c4", family="monospace"),
                    x=0, xanchor="left", pad=dict(l=0, b=4),
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="#071828",
                font=dict(family="monospace", color="#9dc4d8", size=11),
                xaxis=dict(
                    title=None,
                    tickfont=dict(size=12, color="#9dc4d8", family="Georgia,serif"),
                    side="top",
                    tickangle=0,
                    showgrid=False,
                    fixedrange=True,
                ),
                yaxis=dict(
                    title=None,
                    tickfont=dict(size=11, color="#9dc4d8"),
                    autorange="reversed",
                    showgrid=False,
                    fixedrange=True,
                ),
                margin=dict(l=90, r=20, t=56, b=10),
                height=max(200, len(_src_sorted) * 50 + 80),
            )
            st.plotly_chart(_hm_fig, use_container_width=True,
                            config={"displayModeBar": False, "staticPlot": False})

        # ── Chart 2: Theme frequency bar (from Strategic Openings overview) ────
        if _viz_overview.get("themes"):
            _themes = _viz_overview["themes"][:10]
            _th_labels = [t.get("label", "") for t in _themes]
            _th_counts = [int(t.get("count", 1)) for t in _themes]
            _th_sent   = [t.get("dominant_sentiment", "neutral") for t in _themes]
            _sent_color = {"positive": "#16a34a", "negative": "#dc2626", "neutral": "#0a7d8c"}
            _th_colors = [_sent_color.get(s, "#0a7d8c") for s in _th_sent]

            _bar_fig = go.Figure(data=go.Bar(
                x=_th_counts,
                y=_th_labels,
                orientation="h",
                marker=dict(color=_th_colors, line=dict(width=0)),
                text=[str(c) for c in _th_counts],
                textposition="outside",
                textfont=dict(color="#6ea8c4", size=10, family="monospace"),
                hovertemplate="%{y}: %{x} signals<extra></extra>",
            ))
            _bar_topic = f" — {_viz_topic_text[:40]}" if _viz_topic_text else ""
            _bar_fig.update_layout(
                title=dict(
                    text=f"Theme frequency{_bar_topic}",
                    font=dict(size=11, color="#6ea8c4", family="monospace"),
                    x=0, xanchor="left", pad=dict(l=0, b=4),
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="monospace", color="#9dc4d8", size=11),
                xaxis=dict(
                    showgrid=True,
                    gridcolor="#0d2535",
                    zeroline=False,
                    showticklabels=False,
                    fixedrange=True,
                ),
                yaxis=dict(
                    tickfont=dict(size=11, color="#9dc4d8"),
                    autorange="reversed",
                    showgrid=False,
                    fixedrange=True,
                ),
                bargap=0.25,
                margin=dict(l=120, r=50, t=44, b=10),
                height=max(200, len(_themes) * 38 + 60),
                showlegend=False,
            )
            # Sentiment legend
            st.markdown("""
<div style="display:flex;gap:14px;margin-top:4px;margin-bottom:2px;">
  <span style="font-size:10px;color:#16a34a;font-family:monospace;">● positive</span>
  <span style="font-size:10px;color:#dc2626;font-family:monospace;">● negative</span>
  <span style="font-size:10px;color:#0a7d8c;font-family:monospace;">● neutral</span>
</div>""", unsafe_allow_html=True)
            st.plotly_chart(_bar_fig, use_container_width=True,
                            config={"displayModeBar": False, "staticPlot": False})

    except ImportError:
        st.caption("Install `plotly` to enable the Signal Intelligence Map.")
    except Exception as _viz_err:
        pass   # non-fatal — charts are optional

_tr_ctr_research.__exit__(None, None, None)
_tr_ctr_signals.__enter__()


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL LAB — advanced search (folded into Trends tab)
# UI is now the wireframe Search tab above; these functions remain as the
# backend called by the new Run button.
# ══════════════════════════════════════════════════════════════════════════════


@st.cache_data(ttl=900)   # cache 15 min — GDELT rate-limits aggressively
def _gdelt_search(query: str, n: int = 12) -> list:
    """Search GDELT global media database — free, no key needed."""
    import time as _time
    # Simplify query to first 4 words — reduces rate-limit risk on GDELT
    simple_q = " ".join(query.replace(",", "").split()[:4])
    endpoints = [
        # v2 DOC API — primary
        (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={urllib.parse.quote(simple_q)}"
            f"&mode=artlist&maxrecords={n}&format=json&sort=DateDesc"
        ),
        # v2 with English filter — different bucket
        (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={urllib.parse.quote(simple_q + ' sourcelang:english')}"
            f"&mode=artlist&maxrecords={n}&format=json"
        ),
    ]
    for i, url in enumerate(endpoints):
        try:
            if i > 0:
                _time.sleep(2)   # small pause before fallback
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Lighthouse/1.0)"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            articles = data.get("articles") or data.get("results") or []
            if articles:
                return [
                    {
                        "title":    a.get("title", a.get("name", "")),
                        "url":      a.get("url", a.get("htmlurl", "")),
                        "source":   a.get("domain", a.get("sourcename", "")),
                        "seendate": (a.get("seendate") or a.get("date", ""))[:8],
                        "language": a.get("language", ""),
                    }
                    for a in articles
                ]
        except Exception:
            continue
    return [{"error": "GDELT returned no results — try a shorter or simpler query, or wait 30 seconds and try again (rate limit)."}]


def _exa_search(query: str, api_key: str, n: int = 10) -> list:
    """Semantic search via Exa.ai — needs EXA_API_KEY."""
    try:
        from exa_py import Exa
        exa = Exa(api_key=api_key)
        # exa-py ≥1.0: use contents dict, use_autoprompt removed
        res = exa.search(
            query,
            num_results=n,
            contents={"text": {"max_characters": 400}},
        )
        return [
            {
                "title":     r.title or "",
                "url":       r.url or "",
                "snippet":   (getattr(r, "text", None) or "")[:400],
                "published": (getattr(r, "published_date", None) or "")[:10],
            }
            for r in res.results
        ]
    except ImportError:
        return [{"error": "📦 exa-py not installed yet. On Streamlit Cloud: commit requirements.txt and Manage App → Reboot. Locally: pip install exa-py"}]
    except Exception as ex:
        return [{"error": str(ex)}]


def _tavily_search(query: str, api_key: str, n: int = 10) -> list:
    """AI-optimised web search via Tavily — needs TAVILY_API_KEY."""
    try:
        from tavily import TavilyClient
        tc = TavilyClient(api_key=api_key)
        res = tc.search(query, max_results=n, search_depth="advanced")
        return [
            {
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "snippet": r.get("content", "")[:300],
                "score":   round(r.get("score", 0), 2),
            }
            for r in res.get("results", [])
        ]
    except ImportError:
        return [{"error": "📦 tavily-python not installed yet. On Streamlit Cloud: commit the updated requirements.txt and Manage App → Reboot. Locally: pip install tavily-python"}]
    except Exception as ex:
        return [{"error": str(ex)}]


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3 — AD HOC SEARCH (wireframe layout)
# Search bar + platform chip filters + unified results with "+ Add to project"
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""<style>
.srch-res {
    display: flex; align-items: flex-start; justify-content: space-between;
    border: 1px solid #e4e2db; border-radius: 8px;
    padding: 12px 14px; margin-bottom: 8px;
    background: #fff !important;
}
.srch-res-l { flex: 1; min-width: 0; }
.srch-res-r {
    flex: none; margin-left: 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; color: #2f6db0 !important;
    white-space: nowrap; padding-top: 2px;
}
.srch-res-title {
    font-size: 13.5px; font-weight: 500; color: #071828 !important;
    margin-bottom: 4px; line-height: 1.35;
}
.srch-res-snippet {
    font-size: 12px; color: #274d68 !important; line-height: 1.5;
    margin-bottom: 6px;
}
</style>""", unsafe_allow_html=True)

st.markdown("""
<div style="border-top:2px solid #071828;padding-top:1.8rem;margin-top:0.5rem;">
  <span style="font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.18em;
       text-transform:uppercase;color:#0a7d8c;font-weight:700;">🔎 Search</span>
  <div style="font-family:Georgia,serif;font-size:26px;font-weight:600;
       color:#071828;margin:6px 0 4px;">Find currents</div>
  <div style="font-family:Georgia,serif;font-style:italic;font-size:14px;color:#274d68;
       max-width:64ch;margin-bottom:0;">Ad hoc signal search — one query across all connected sources. Collect anything useful straight into a project folder.</div>
</div>
""", unsafe_allow_html=True)

# ── Search bar + Run ─────────────────────────────────────────────────────────
_sq_col, _sq_run = st.columns([8, 1])
with _sq_col:
    lab_query = st.text_input(
        "Find currents about…",
        value=focus_topic.split(",")[0].strip() if focus_topic else "",
        placeholder="Find currents about…",
        key="lab_query",
        label_visibility="collapsed",
    )
with _sq_run:
    _run_search = st.button("Run", use_container_width=True, type="primary", key="btn_run_search")

# ── Platform chips + API settings ─────────────────────────────────────────────
_exa_key  = os.environ.get("EXA_API_KEY", "")
_tav_key  = os.environ.get("TAVILY_API_KEY", "")
_lab_brand = client_name.split("·")[0].strip()

_srch_col_a, _srch_col_b, _srch_col_c, _srch_col_d = st.columns(4)
with _srch_col_a:
    _use_local  = st.checkbox("📡 Ingested signals", value=True, key="srch_local")
with _srch_col_b:
    _use_gdelt  = st.checkbox("🌍 GDELT", value=True, key="srch_gdelt")
with _srch_col_c:
    _use_exa    = st.checkbox(f"🧠 Exa.ai{'  ✓' if _exa_key else ''}", value=bool(_exa_key), key="srch_exa", disabled=not _exa_key)
with _srch_col_d:
    _use_tavily = st.checkbox(f"⚡ Tavily{'  ✓' if _tav_key else ''}", value=bool(_tav_key), key="srch_tav", disabled=not _tav_key)

# Defaults (overridden inside the settings expander below)
_exa_key_input = _exa_key
_tav_key_input = _tav_key
_srch_n        = 8
_brand_focus   = True

with st.expander("⚙ API keys & settings", expanded=not (_exa_key and _tav_key)):
    _kc1, _kc2, _kc3 = st.columns(3)
    with _kc1:
        if _exa_key:
            st.success("✓ EXA_API_KEY loaded")
        else:
            _exa_key_input = st.text_input("EXA_API_KEY", type="password",
                help="Get a free key at exa.ai", key="srch_exa_key")
    with _kc2:
        if _tav_key:
            st.success("✓ TAVILY_API_KEY loaded")
        else:
            _tav_key_input = st.text_input("TAVILY_API_KEY", type="password",
                help="Get a free key at tavily.com", key="srch_tav_key")
    with _kc3:
        _srch_n      = st.slider("Results per source", 4, 15, 8, key="srch_n")
        _brand_focus = st.checkbox(f"🎯 Brand focus ({_lab_brand})", value=True,
                                   key="srch_brand", help=f"Prepend '{_lab_brand}' to query")

effective_query = f"{_lab_brand} {lab_query}" if _brand_focus and lab_query else lab_query

# ── Run search ────────────────────────────────────────────────────────────────
if _run_search and lab_query:
    _srch_results = {}
    with st.spinner(f'Searching for \"{effective_query[:50]}\"…'):
        if _use_local:
            _sigs = load_signals()
            _q_lower = effective_query.lower()
            _local_hits = [
                s for s in _sigs
                if any(w in f"{s.get('title','')} {s.get('content','')}".lower()
                       for w in _q_lower.split()[:4])
            ][:_srch_n]
            _srch_results["local"] = _local_hits
        if _use_gdelt:
            _srch_results["gdelt"] = _gdelt_search(effective_query, _srch_n)
        if _use_exa and _exa_key_input:
            _srch_results["exa"] = _exa_search(effective_query, _exa_key_input, _srch_n)
        if _use_tavily and _tav_key_input:
            _srch_results["tavily"] = _tavily_search(effective_query, _tav_key_input, _srch_n)
    st.session_state["srch_results"]  = _srch_results
    st.session_state["srch_query"]    = effective_query

# ── Results (wireframe left/right layout) ─────────────────────────────────────
_srch_display = st.session_state.get("srch_results")
if _srch_display:
    st.markdown(f"""
<div style="font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.1em;
text-transform:uppercase;color:#9dc4d8;margin:18px 0 10px;">
Results · {e(st.session_state.get('srch_query',''))}</div>""", unsafe_allow_html=True)

    _proj_folders_srch = load_project_folders()
    _res_idx = 0

    def _render_srch_result(title, snippet, chip_meta, url, source_key, res_idx):
        """Render one wireframe-style result row with + Add to project."""
        _link_html = (
            f'<a href="{e(url)}" target="_blank" '
            f'style="font-family:JetBrains Mono,monospace;font-size:9px;'
            f'color:#0a7d8c;text-decoration:none;">↗ open</a>'
        ) if url else ""
        st.markdown(f"""
<div class="srch-res">
  <div class="srch-res-l">
    <div class="srch-res-title">{e(title)}</div>
    {'<div class="srch-res-snippet">' + e(snippet[:180]) + '…</div>' if snippet else ''}
    <span class="chip-meta">{e(chip_meta)}</span>  {_link_html}
  </div>
</div>""", unsafe_allow_html=True)
        if not IS_CLIENT and _proj_folders_srch:
            _save_button(
                "Add", f"Search Result · {source_key}",
                title[:120],
                f"{chip_meta}\n{url}\n\n{snippet[:300]}",
                key=f"srch_add_{res_idx}",
                user=st.session_state.logged_in_user,
            )

    # Local signals
    _local_res = _srch_display.get("local", [])
    if _local_res:
        st.markdown("""<div style="font-family:'JetBrains Mono',monospace;font-size:9px;
letter-spacing:.14em;text-transform:uppercase;color:#0a7d8c;font-weight:700;
margin:14px 0 6px;">📡 Ingested signals</div>""", unsafe_allow_html=True)
        for _r in _local_res:
            _src  = _r.get("source", "").title()
            _date = (_r.get("date") or _r.get("scraped_at") or "")[:10]
            _render_srch_result(
                _r.get("title", "")[:90],
                _r.get("content", "")[:180],
                f"{_src} · {_date}".strip(" ·"),
                _r.get("url", ""),
                "Ingested",
                _res_idx,
            )
            _res_idx += 1

    # GDELT
    _gdelt_res = _srch_display.get("gdelt", [])
    if _gdelt_res and not ("error" in (_gdelt_res[0] if _gdelt_res else {})):
        st.markdown("""<div style="font-family:'JetBrains Mono',monospace;font-size:9px;
letter-spacing:.14em;text-transform:uppercase;color:#6ea8c4;font-weight:700;
margin:14px 0 6px;">🌍 GDELT · global media</div>""", unsafe_allow_html=True)
        for _r in _gdelt_res:
            _render_srch_result(
                _r.get("title", "")[:90],
                "",
                f"{_r.get('source','')} · {_r.get('seendate','')[:8]}".strip(" ·"),
                _r.get("url", ""),
                "GDELT",
                _res_idx,
            )
            _res_idx += 1
    elif _gdelt_res and "error" in _gdelt_res[0]:
        st.caption(f"GDELT: {_gdelt_res[0]['error']}")

    # Exa
    _exa_res = _srch_display.get("exa", [])
    if _exa_res and not ("error" in (_exa_res[0] if _exa_res else {})):
        st.markdown("""<div style="font-family:'JetBrains Mono',monospace;font-size:9px;
letter-spacing:.14em;text-transform:uppercase;color:#0a7d8c;font-weight:700;
margin:14px 0 6px;">🧠 Exa.ai · semantic search</div>""", unsafe_allow_html=True)
        for _r in _exa_res:
            _render_srch_result(
                _r.get("title", "")[:90],
                _r.get("snippet", "")[:180],
                f"Exa · {_r.get('published','')[:10]}".strip(" ·"),
                _r.get("url", ""),
                "Exa.ai",
                _res_idx,
            )
            _res_idx += 1
    elif _exa_res and "error" in _exa_res[0]:
        st.caption(f"Exa.ai: {_exa_res[0]['error']}")

    # Tavily
    _tav_res = _srch_display.get("tavily", [])
    if _tav_res and not ("error" in (_tav_res[0] if _tav_res else {})):
        st.markdown("""<div style="font-family:'JetBrains Mono',monospace;font-size:9px;
letter-spacing:.14em;text-transform:uppercase;color:#274d68;font-weight:700;
margin:14px 0 6px;">⚡ Tavily · AI-optimised web</div>""", unsafe_allow_html=True)
        for _r in _tav_res:
            _render_srch_result(
                _r.get("title", "")[:90],
                _r.get("snippet", "")[:180],
                f"Tavily · score {_r.get('score','')}",
                _r.get("url", ""),
                "Tavily",
                _res_idx,
            )
            _res_idx += 1
    elif _tav_res and "error" in _tav_res[0]:
        st.caption(f"Tavily: {_tav_res[0]['error']}")

    if _res_idx == 0:
        st.info("No results found — try a different query or enable more sources.")

# ── Keep old lab tabs hidden (for reference) — never displayed ─────────────────
if False:
    lab_tab_cur = lab_tab_unified = None  # dead code placeholder


# ── OLD Lab tabs — replaced by wireframe Search UI above. Kept in `if False`
# so they don't run but the code is preserved for reference.
if False:
    _dead_cur = True
if False:  # old lab_tab_cur block — never executed
    _sigs = load_signals()
    st.markdown("""
<div style="font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.14em;
text-transform:uppercase;color:#0a7d8c;font-weight:700;margin-bottom:14px;">
Current ingestion pipeline · Apify scraping</div>""", unsafe_allow_html=True)

    src_counts = {}
    for s in _sigs:
        src = s.get("source", "other")
        src_counts[src] = src_counts.get(src, 0) + 1

    stat_cols = st.columns(len(src_counts) + 1)
    with stat_cols[0]:
        st.metric("Total signals", f"{len(_sigs):,}")
    for i, (src, cnt) in enumerate(sorted(src_counts.items(), key=lambda x: -x[1])):
        with stat_cols[i + 1]:
            st.metric(src.title(), f"{cnt:,}")

    st.markdown("---")
    st.markdown("""
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
<div style="background:#fff;border:1px solid #9dc4d8;border-left:3px solid #0a7d8c;border-radius:8px;padding:14px 16px;">
  <div style="font-family:'JetBrains Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:#0a7d8c;margin-bottom:6px;">✓ Strengths</div>
  <ul style="font-size:12.5px;color:#274d68;line-height:1.8;padding-left:16px;margin:0;">
    <li>1,100+ real signals already ingested</li>
    <li>Reddit threads with full comment context</li>
    <li>TikTok video descriptions + metadata</li>
    <li>RSS articles with full text</li>
    <li>Consistent schema across all sources</li>
  </ul>
</div>
<div style="background:#fff;border:1px solid #9dc4d8;border-left:3px solid #c94f35;border-radius:8px;padding:14px 16px;">
  <div style="font-family:'JetBrains Mono',monospace;font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:#c94f35;margin-bottom:6px;">△ Limitations</div>
  <ul style="font-size:12.5px;color:#274d68;line-height:1.8;padding-left:16px;margin:0;">
    <li>Keyword-based — misses conceptual matches</li>
    <li>Platform-specific scrapers break on updates</li>
    <li>No real-time streaming — batch only</li>
    <li>Limited to pre-configured sources</li>
    <li>No semantic relevance ranking</li>
  </ul>
</div>
</div>""", unsafe_allow_html=True)

    st.markdown("<br>**Sample from current signals matching your query:**", unsafe_allow_html=True)
    q_lower = effective_query.lower()
    matches = [
        s for s in _sigs
        if any(w in f"{s.get('title','')} {s.get('content','')}".lower()
               for w in q_lower.split()[:4])
    ][:6]
    if matches:
        m_cols = st.columns(3)
        for i, s in enumerate(matches):
            with m_cols[i % 3]:
                color   = SOURCE_COLORS.get(s.get("source", "web"), "#9dc4d8")
                _s_url  = s.get("url", "")
                _s_link = (
                    '<a href="' + e(_s_url) + '" target="_blank" '
                    'style="font-family:JetBrains Mono,monospace;font-size:9px;color:' + color +
                    ';text-decoration:none;margin-top:8px;display:inline-block;">↗ source</a>'
                ) if _s_url else ""
                st.markdown(f"""
<div style="background:rgba(255,255,255,.8);border:1px solid #9dc4d8;border-left:3px solid {color};
border-radius:6px;padding:12px;margin-bottom:10px;">
  <div style="font-family:'JetBrains Mono',monospace;font-size:8.5px;text-transform:uppercase;
  color:{color};margin-bottom:6px;">● {s.get('source','').title()}</div>
  <div style="font-size:12.5px;font-weight:600;color:#071828;margin-bottom:6px;line-height:1.3;">
    {e(s.get('title','')[:70])}</div>
  <div style="font-size:11.5px;color:#274d68;line-height:1.5;">
    {e(s.get('content','')[:120])}…</div>
  {_s_link}
</div>""", unsafe_allow_html=True)
    else:
        st.caption("No keyword matches found — try a different query.")


if False:  # old lab_tab_unified block — never executed
    import re as _re

    st.markdown("""
<div style="font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.14em;
text-transform:uppercase;color:#0a7d8c;font-weight:700;margin-bottom:4px;">
🔎 Unified search · Exa · GDELT · Tavily · YouTube</div>
<div style="font-size:13px;color:#274d68;line-height:1.6;margin-bottom:16px;max-width:64ch;">
One query, every next-gen source at once. Compare semantic search (Exa), global media (GDELT),
AI-optimised search (Tavily) and video signals (YouTube) side by side — grouped below.</div>
""", unsafe_allow_html=True)

    def _yt_oembed(url: str) -> dict:
        """Fetch video metadata via YouTube oEmbed — free, no API key."""
        oe_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
        req = urllib.request.Request(oe_url, headers={"User-Agent": "Lighthouse/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    def _extract_vid_id(url: str) -> str:
        m = _re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
        return m.group(1) if m else ""

    def _load_yt_cards(urls: list) -> list:
        cards = []
        for url in urls[:12]:
            try:
                meta = _yt_oembed(url)
                cards.append({
                    "title":   meta.get("title", ""),
                    "channel": meta.get("author_name", ""),
                    "thumb":   meta.get("thumbnail_url", ""),
                    "url":     url,
                    "vid_id":  _extract_vid_id(url),
                })
            except Exception as ex:
                cards.append({"title": f"Could not load: {url[:50]}", "error": str(ex), "url": url})
        return cards

    exa_key = os.environ.get("EXA_API_KEY", "")
    tav_key = os.environ.get("TAVILY_API_KEY", "")

    with st.expander("⚙ API keys & settings", expanded=not (exa_key and tav_key)):
        kcol1, kcol2, kcol3 = st.columns(3)
        with kcol1:
            if exa_key:
                st.success("✓ EXA_API_KEY loaded")
                exa_key_input = exa_key
            else:
                exa_key_input = st.text_input(
                    "EXA_API_KEY", type="password",
                    help="Get free key at exa.ai — 1,000 searches/month free",
                    key="unified_exa_key",
                )
        with kcol2:
            if tav_key:
                st.success("✓ TAVILY_API_KEY loaded")
                tav_key_input = tav_key
            else:
                tav_key_input = st.text_input(
                    "TAVILY_API_KEY", type="password",
                    help="Get free key at tavily.com — 1,000 searches/month free",
                    key="unified_tav_key",
                )
        with kcol3:
            unified_n = st.slider("Results per source", 4, 15, 8, key="unified_n")

    src_enabled = {
        "Exa.ai":  bool(exa_key_input),
        "GDELT":   True,
        "Tavily":  bool(tav_key_input),
        "YouTube": bool(exa_key_input),
    }
    enabled_list = " · ".join(s for s, v in src_enabled.items() if v)
    missing_note = "" if (exa_key_input and tav_key_input) else " — add the missing API key(s) above to unlock more sources"
    st.caption(f"Will search: {enabled_list}{missing_note}")

    if st.button("🔍 Search all sources", key="btn_unified_search", type="primary"):
        results = {}
        with st.spinner(f"Searching {sum(src_enabled.values())} sources for \"{effective_query[:50]}\"…"):
            results["GDELT"] = _gdelt_search(effective_query, unified_n)

            if exa_key_input:
                results["Exa.ai"] = _exa_search(effective_query, exa_key_input, unified_n)

            if tav_key_input:
                results["Tavily"] = _tavily_search(effective_query, tav_key_input, unified_n)

            if exa_key_input:
                try:
                    from exa_py import Exa
                    _exa = Exa(api_key=exa_key_input)
                    _yt_res = _exa.search(
                        effective_query,
                        num_results=min(unified_n, 9),
                        include_domains=["youtube.com"],
                        type="neural",
                    )
                    _yt_urls = [r.url for r in _yt_res.results
                                if "watch" in r.url or "youtu.be" in r.url]
                    results["YouTube"] = _load_yt_cards(_yt_urls)
                except ImportError:
                    results["YouTube"] = [{"error": "📦 exa-py not installed. Commit requirements.txt and reboot Streamlit Cloud."}]
                except Exception as ex:
                    results["YouTube"] = [{"error": str(ex)}]

        st.session_state["unified_results"] = results
        st.session_state["unified_query"] = effective_query

    unified_results = st.session_state.get("unified_results")
    if unified_results:
        st.markdown("---")
        st.markdown(f"**Results for:** `{e(st.session_state.get('unified_query',''))}`")

        # ---- GDELT ----
        gdelt_results = unified_results.get("GDELT", [])
        if gdelt_results and "error" in gdelt_results[0]:
            st.caption(f"GDELT: {gdelt_results[0]['error']}")
        elif gdelt_results:
            st.markdown(f"""<div class="lh-cat-head"><span class="lh-cat-line"></span>
<span class="lh-cat-lbl" style="color:#6ea8c4;">🌍 GDELT · {len(gdelt_results)} articles</span>
<span class="lh-cat-line"></span></div>""", unsafe_allow_html=True)
            g_cols = st.columns(2)
            for i, r in enumerate(gdelt_results):
                with g_cols[i % 2]:
                    lang = r.get("language", "")
                    lang_tag = f'<span style="font-size:8px;background:#ebf2f7;padding:1px 6px;border-radius:3px;color:#274d68;">{lang}</span>' if lang else ""
                    st.markdown(f"""
<div style="background:#fff;border:1px solid #9dc4d8;border-left:3px solid #6ea8c4;
border-radius:6px;padding:12px;margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
    <div style="font-family:'JetBrains Mono',monospace;font-size:9px;color:#6ea8c4;">{e(r.get('source',''))}</div>
    <div style="display:flex;gap:6px;align-items:center;">
      {lang_tag}
      <span style="font-family:'JetBrains Mono',monospace;font-size:9px;color:#9dc4d8;">{r.get('seendate','')}</span>
    </div>
  </div>
  <div style="font-size:13px;font-weight:600;color:#071828;line-height:1.3;margin-bottom:8px;">
    {e(r.get('title','')[:90])}</div>
  <a href="{e(r.get('url',''))}" target="_blank"
  style="font-family:'JetBrains Mono',monospace;font-size:9px;color:#6ea8c4;text-decoration:none;">
  ↗ read article</a>
</div>""", unsafe_allow_html=True)
                    if not IS_CLIENT:
                        gb_col, _ = st.columns([1, 6])
                        with gb_col:
                            _save_button(
                                "Add", "Search Result · GDELT",
                                r.get("title", "")[:120],
                                f"{r.get('source','')} — {r.get('seendate','')}\n{r.get('url','')}",
                                key=f"unif_gdelt_{i}",
                                user=st.session_state.logged_in_user,
                            )
        else:
            st.caption("GDELT: no results for this query.")

        # ---- Exa ----
        exa_results = unified_results.get("Exa.ai")
        if exa_results is not None:
            if exa_results and "error" in exa_results[0]:
                st.caption(f"Exa.ai: {exa_results[0]['error']}")
            elif exa_results:
                st.markdown(f"""<div class="lh-cat-head"><span class="lh-cat-line"></span>
<span class="lh-cat-lbl" style="color:#0a7d8c;">🧠 Exa.ai · {len(exa_results)} semantic results</span>
<span class="lh-cat-line"></span></div>""", unsafe_allow_html=True)
                for i, r in enumerate(exa_results):
                    st.markdown(f"""
<div style="background:#fff;border:1px solid #9dc4d8;border-left:3px solid #0a7d8c;
border-radius:6px;padding:12px 14px;margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
    <div style="font-size:13px;font-weight:600;color:#071828;">{e(r.get('title','')[:80])}</div>
    <div style="font-family:'JetBrains Mono',monospace;font-size:9px;color:#9dc4d8;">{r.get('published','')}</div>
  </div>
  <div style="font-size:12px;color:#274d68;line-height:1.55;margin-bottom:8px;">{e(r.get('snippet',''))}</div>
  <a href="{e(r.get('url',''))}" target="_blank" style="font-family:'JetBrains Mono',monospace;
  font-size:9px;color:#0a7d8c;text-decoration:none;">↗ {e(r.get('url','')[:60])}</a>
</div>""", unsafe_allow_html=True)
                    if not IS_CLIENT:
                        eb_col, _ = st.columns([1, 6])
                        with eb_col:
                            _save_button(
                                "Add", "Search Result · Exa.ai",
                                r.get("title", "")[:120],
                                f"{r.get('snippet','')}\n{r.get('url','')}",
                                key=f"unif_exa_{i}",
                                user=st.session_state.logged_in_user,
                            )
            else:
                st.caption("Exa.ai: no results for this query.")

        # ---- Tavily ----
        tav_results = unified_results.get("Tavily")
        if tav_results is not None:
            if tav_results and "error" in tav_results[0]:
                st.caption(f"Tavily: {tav_results[0]['error']}")
            elif tav_results:
                st.markdown(f"""<div class="lh-cat-head"><span class="lh-cat-line"></span>
<span class="lh-cat-lbl" style="color:#0fa3b5;">⚡ Tavily · {len(tav_results)} AI-optimised results</span>
<span class="lh-cat-line"></span></div>""", unsafe_allow_html=True)
                for i, r in enumerate(tav_results):
                    score = r.get("score", 0)
                    score_color = "#1a8a6b" if score > 0.7 else "#0a7d8c" if score > 0.4 else "#9dc4d8"
                    st.markdown(f"""
<div style="background:#fff;border:1px solid #9dc4d8;border-left:3px solid #0fa3b5;
border-radius:6px;padding:12px 14px;margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
    <div style="font-size:13px;font-weight:600;color:#071828;">{e(r.get('title','')[:80])}</div>
    <div style="font-family:'JetBrains Mono',monospace;font-size:9px;color:{score_color};
    background:rgba(10,125,140,.08);padding:2px 6px;border-radius:3px;">
    rel {score}</div>
  </div>
  <div style="font-size:12px;color:#274d68;line-height:1.55;margin-bottom:8px;">
    {e(r.get('snippet',''))}</div>
  <a href="{e(r.get('url',''))}" target="_blank"
  style="font-family:'JetBrains Mono',monospace;font-size:9px;color:#0fa3b5;text-decoration:none;">
  ↗ {e(r.get('url','')[:60])}</a>
</div>""", unsafe_allow_html=True)
                    if not IS_CLIENT:
                        tb_col, _ = st.columns([1, 6])
                        with tb_col:
                            _save_button(
                                "Add", "Search Result · Tavily",
                                r.get("title", "")[:120],
                                f"{r.get('snippet','')}\n{r.get('url','')}",
                                key=f"unif_tavily_{i}",
                                user=st.session_state.logged_in_user,
                            )
            else:
                st.caption("Tavily: no results for this query.")

        # ---- YouTube ----
        yt_results = unified_results.get("YouTube")
        if yt_results is not None:
            valid_yt = [c for c in yt_results if "error" not in c]
            if valid_yt:
                st.markdown(f"""<div class="lh-cat-head"><span class="lh-cat-line"></span>
<span class="lh-cat-lbl" style="color:#d44800;">🎥 YouTube · {len(valid_yt)} videos</span>
<span class="lh-cat-line"></span></div>""", unsafe_allow_html=True)
                yt_cols = st.columns(3, gap="medium")
                for i, c in enumerate(valid_yt):
                    with yt_cols[i % 3]:
                        st.markdown(f"""
<div style="background:#fff;border:1px solid #9dc4d8;border-left:3px solid #d44800;
border-radius:8px;overflow:hidden;margin-bottom:14px;">
  <img src="{e(c['thumb'])}" style="width:100%;display:block;"/>
  <div style="padding:12px 14px;">
    <div style="font-size:13px;font-weight:600;color:#071828;line-height:1.3;margin-bottom:6px;">
      {e(c['title'][:80])}</div>
    <div style="font-family:'JetBrains Mono',monospace;font-size:9px;text-transform:uppercase;
    letter-spacing:.08em;color:#d44800;margin-bottom:10px;">{e(c['channel'])}</div>
    <a href="{e(c['url'])}" target="_blank"
    style="font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.06em;
    text-transform:uppercase;color:#d44800;text-decoration:none;
    border-bottom:1px solid #d44800;padding-bottom:1px;">↗ watch on YouTube</a>
  </div>
</div>""", unsafe_allow_html=True)
                        if not IS_CLIENT:
                            _save_button(
                                "Add", "Search Result · YouTube",
                                c.get("title", "")[:120],
                                f"{c.get('channel','')}\n{c.get('url','')}",
                                key=f"unif_yt_{i}",
                                user=st.session_state.logged_in_user,
                            )
            elif yt_results:
                st.caption(f"YouTube: {yt_results[0].get('error', 'no results for this query.')}")
            else:
                st.caption("YouTube: no results for this query.")
    else:
        st.caption('Click "Search all sources" to run the unified query across every available engine.')

    with st.expander("Source guide — when to use which"):
        st.markdown("""
| Source | Best for | Notes |
|---|---|---|
| **GDELT** | Macro trend validation in global media | Free, no key, 65+ languages, updated every 15 min |
| **Exa.ai** | Conceptual/semantic discovery — "find conversations about X" | Free tier: 1,000/month |
| **Tavily** | Recent news & facts, AI-optimised, relevance-scored | Free tier: 1,000/month |
| **YouTube** | Video signals — creators setting the agenda before it's written about | Powered by Exa neural search, no separate key |

**Recommendation:** run all sources together. GDELT validates scale and global reach, Exa and Tavily surface conceptual and factual angles, YouTube catches what's brewing visually before it goes viral. Feed the combined results into Claude for the richest possible context.

**Pipeline integration:** add transcripts via `youtube-transcript-api`, chunk into ~500-word segments, and embed alongside the other sources for the deepest searchable signal base.
""")

_tr_ctr_signals.__exit__(None, None, None)
tab_roadmap.__enter__()


# ══════════════════════════════════════════════════════════════════════════════
# VISION MAP — strategic roadmap embedded
# ══════════════════════════════════════════════════════════════════════════════

st.markdown('<div id="lh-sec-vision"></div>', unsafe_allow_html=True)
st.markdown("""
<div style="border-top:3px double #071828;padding-top:2rem;margin-top:1rem;">
  <span style="font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.18em;
       text-transform:uppercase;color:#0a7d8c;font-weight:700;">◎ Product Vision</span>
  <div style="font-family:'Georgia',serif;font-size:28px;font-weight:600;
       color:#071828;margin:8px 0 6px;">The Lighthouse Roadmap</div>
  <div style="font-family:'Georgia',serif;font-style:italic;font-size:14px;color:#274d68;
       margin-bottom:20px;">From prototype to cultural intelligence platform — the strategic vision
       and execution roadmap.</div>
</div>
""", unsafe_allow_html=True)

_VISION_MAP_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#062233;font-family:'Georgia',serif;color:#d0eaf0;overflow-x:hidden;}
nav{display:flex;gap:0;border-bottom:1px solid rgba(255,255,255,.1);background:#041a28;}
nav button{flex:1;padding:11px 6px;background:transparent;border:none;
  font-family:'JetBrains Mono',monospace,sans-serif;font-size:9.5px;letter-spacing:.1em;
  text-transform:uppercase;color:rgba(208,234,240,.4);cursor:pointer;
  border-bottom:2px solid transparent;transition:all .2s;}
nav button.active{color:#0fa3b5;border-bottom-color:#0fa3b5;}
nav button:hover:not(.active){color:rgba(208,234,240,.75);}
.panel{display:none;padding:22px 28px;min-height:380px;}
.panel.active{display:block;}
.eyebrow{font-family:'JetBrains Mono',monospace,sans-serif;font-size:9px;letter-spacing:.2em;
  text-transform:uppercase;color:#0fa3b5;font-weight:700;margin-bottom:8px;}
.big-title{font-size:22px;font-weight:600;line-height:1.2;color:#fff;margin-bottom:8px;}
.lead{font-style:italic;font-size:13px;color:rgba(208,234,240,.6);line-height:1.6;max-width:620px;margin-bottom:20px;}
.grid{display:grid;gap:12px;}
.grid-2{grid-template-columns:1fr 1fr;}
.grid-3{grid-template-columns:1fr 1fr 1fr;}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
  border-radius:8px;padding:14px 16px;transition:.2s;}
.card:hover{background:rgba(255,255,255,.09);border-color:rgba(15,163,181,.4);}
.card-icon{font-size:18px;margin-bottom:7px;}
.card-title{font-family:'JetBrains Mono',monospace,sans-serif;font-size:9.5px;
  letter-spacing:.1em;text-transform:uppercase;color:#0fa3b5;margin-bottom:5px;}
.card-body{font-size:12px;color:rgba(208,234,240,.65);line-height:1.5;}
.card-tag{display:inline-block;margin-top:7px;font-family:'JetBrains Mono',monospace,sans-serif;
  font-size:8px;letter-spacing:.06em;text-transform:uppercase;
  padding:2px 7px;border-radius:20px;}
.tag-now{background:rgba(10,125,140,.3);color:#0fa3b5;border:1px solid rgba(10,125,140,.5);}
.tag-next{background:rgba(26,138,107,.2);color:#1a8a6b;border:1px solid rgba(26,138,107,.4);}
.tag-future{background:rgba(201,79,53,.2);color:#c94f35;border:1px solid rgba(201,79,53,.4);}
.timeline{position:relative;padding-left:22px;}
.timeline::before{content:'';position:absolute;left:7px;top:0;bottom:0;width:1px;background:rgba(255,255,255,.12);}
.tl-item{position:relative;margin-bottom:20px;}
.tl-dot{position:absolute;left:-18px;top:4px;width:8px;height:8px;border-radius:50%;flex:none;}
.tl-label{font-family:'JetBrains Mono',monospace,sans-serif;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:#0fa3b5;margin-bottom:3px;}
.tl-title{font-size:14px;font-weight:600;color:#fff;margin-bottom:3px;}
.tl-body{font-size:11.5px;color:rgba(208,234,240,.55);line-height:1.5;}
.metaphor-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:8px;}
.m-card{border-radius:8px;padding:12px 14px;border:1px solid;}
.m-icon{font-size:20px;margin-bottom:5px;}
.m-name{font-family:'JetBrains Mono',monospace,sans-serif;font-size:9px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;margin-bottom:4px;}
.m-desc{font-size:11px;line-height:1.5;color:rgba(208,234,240,.6);}
.eco-row{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap;}
.eco-pill{padding:4px 10px;border-radius:20px;font-family:'JetBrains Mono',monospace,sans-serif;font-size:8.5px;letter-spacing:.07em;text-transform:uppercase;border:1px solid;}
.eco-active{background:rgba(10,125,140,.2);color:#0fa3b5;border-color:rgba(10,125,140,.5);}
.eco-next{background:rgba(26,138,107,.1);color:#1a8a6b;border-color:rgba(26,138,107,.35);}
.eco-future{background:rgba(110,168,196,.08);color:rgba(110,168,196,.8);border-color:rgba(110,168,196,.25);}
.eco-label{font-family:'JetBrains Mono',monospace,sans-serif;font-size:8.5px;letter-spacing:.1em;text-transform:uppercase;color:rgba(208,234,240,.3);margin-bottom:5px;margin-top:12px;}
.legend{display:flex;gap:14px;margin-bottom:16px;flex-wrap:wrap;}
.leg-item{display:flex;align-items:center;gap:5px;font-family:'JetBrains Mono',monospace,sans-serif;font-size:8.5px;letter-spacing:.05em;text-transform:uppercase;color:rgba(208,234,240,.5);}
.leg-dot{width:7px;height:7px;border-radius:50%;}
</style>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
</head><body>
<nav>
  <button class="active" onclick="show('ns',this)">North Star</button>
  <button onclick="show('data',this)">Data Layer</button>
  <button onclick="show('intel',this)">Intelligence</button>
  <button onclick="show('exp',this)">Experience</button>
  <button onclick="show('road',this)">Roadmap</button>
</nav>

<div class="panel active" id="ns">
  <div class="eyebrow">The Vision</div>
  <div class="big-title">A living cultural intelligence organism</div>
  <div class="lead">Not a dashboard. Not a listening tool. A strategic partner — showing teams not just what is happening in culture, but what it means, what's coming, and what to do.</div>
  <div class="metaphor-grid">
    <div class="m-card" style="background:rgba(10,125,140,.08);border-color:rgba(10,125,140,.3);">
      <div class="m-icon">🌊</div><div class="m-name" style="color:#0fa3b5;">The Currents</div>
      <div class="m-desc">What culture is already moving toward. Forces brands ignore at their peril.</div>
    </div>
    <div class="m-card" style="background:rgba(201,79,53,.08);border-color:rgba(201,79,53,.3);">
      <div class="m-icon">⬆️</div><div class="m-name" style="color:#c94f35;">The Countercurrents</div>
      <div class="m-desc">The deliberate move against the flow. Unowned territory where brave brands build lasting distinction.</div>
    </div>
    <div class="m-card" style="background:rgba(26,138,107,.08);border-color:rgba(26,138,107,.3);">
      <div class="m-icon">🪸</div><div class="m-name" style="color:#1a8a6b;">The Rocks</div>
      <div class="m-desc">Hidden dangers. Crisis signals before they become crises. The early warning system.</div>
    </div>
    <div class="m-card" style="background:rgba(157,196,216,.08);border-color:rgba(157,196,216,.25);">
      <div class="m-icon">⚓</div><div class="m-name" style="color:#9dc4d8;">The Harbour</div>
      <div class="m-desc">Cultural territory the brand already owns. The safe base before venturing into open water.</div>
    </div>
    <div class="m-card" style="background:rgba(110,168,196,.08);border-color:rgba(110,168,196,.25);">
      <div class="m-icon">🌫️</div><div class="m-name" style="color:#6ea8c4;">The Fog</div>
      <div class="m-desc">Ambiguous signals needing human judgment. Weak signals that could be the next big thing — or noise.</div>
    </div>
    <div class="m-card" style="background:rgba(208,234,240,.04);border-color:rgba(208,234,240,.12);">
      <div class="m-icon">🌅</div><div class="m-name" style="color:rgba(208,234,240,.65);">The Open Sea</div>
      <div class="m-desc">Unexplored cultural territory. White space no brand has claimed. Visible only from the lighthouse beam.</div>
    </div>
  </div>
</div>

<div class="panel" id="data">
  <div class="eyebrow">Signal Ecosystem</div>
  <div class="big-title">Expanding the antenna</div>
  <div class="lead">Intelligence richness is proportional to signal breadth and depth. A phased expansion plan.</div>
  <div class="legend">
    <div class="leg-item"><div class="leg-dot" style="background:#0fa3b5;"></div>Active</div>
    <div class="leg-item"><div class="leg-dot" style="background:#1a8a6b;"></div>Next quarter</div>
    <div class="leg-item"><div class="leg-dot" style="background:#6ea8c4;"></div>Six months+</div>
  </div>
  <div class="eco-label">Social & Community</div>
  <div class="eco-row">
    <div class="eco-pill eco-active">Reddit</div><div class="eco-pill eco-active">TikTok</div>
    <div class="eco-pill eco-next">YouTube Transcripts</div><div class="eco-pill eco-next">X / Twitter</div>
    <div class="eco-pill eco-next">Mumsnet</div><div class="eco-pill eco-future">LinkedIn</div>
    <div class="eco-pill eco-future">Discord</div>
  </div>
  <div class="eco-label">Search & Semantic</div>
  <div class="eco-row">
    <div class="eco-pill eco-next">Exa.ai</div><div class="eco-pill eco-next">Tavily</div>
    <div class="eco-pill eco-next">SerpAPI</div><div class="eco-pill eco-future">Google Trends</div>
    <div class="eco-pill eco-future">App Store reviews</div>
  </div>
  <div class="eco-label">Media & Intelligence</div>
  <div class="eco-row">
    <div class="eco-active eco-pill">RSS</div><div class="eco-pill eco-next">GDELT (global media)</div>
    <div class="eco-pill eco-next">Podcast transcripts</div><div class="eco-pill eco-future">Newsletter archives</div>
    <div class="eco-pill eco-future">Patent filings</div>
  </div>
</div>

<div class="panel" id="intel">
  <div class="eyebrow">AI Layer</div>
  <div class="big-title">From data to foresight</div>
  <div class="lead">Competitive advantage is not more signals — it is deeper intelligence extracted from them.</div>
  <div class="grid grid-2">
    <div class="card"><div class="card-icon">🌡️</div><div class="card-title">Cultural Temperature</div>
      <div class="card-body">Sentiment at signal level — not positive/negative, but nuanced: ironic, anxious, aspirational, nostalgic. Emotional temperature of a category over time.</div>
      <span class="card-tag tag-next">Next</span></div>
    <div class="card"><div class="card-icon">📡</div><div class="card-title">Anomaly Detection</div>
      <div class="card-body">Auto-alerts when signal volume spikes unexpectedly. 2σ above rolling baseline triggers a Lighthouse Alert before anyone else sees it.</div>
      <span class="card-tag tag-next">Next</span></div>
    <div class="card"><div class="card-icon">🧬</div><div class="card-title">Narrative Clustering</div>
      <div class="card-body">Group signals into coherent "stories" spreading across platforms — not just topics, but the narrative arc: problem → reaction → meme.</div>
      <span class="card-tag tag-next">Next</span></div>
    <div class="card"><div class="card-icon">🔮</div><div class="card-title">Momentum Forecasting</div>
      <div class="card-body">Is this topic at 20% of its peak, or 80%? Gives teams a timing signal — when to move, when to hold.</div>
      <span class="card-tag tag-future">6 months</span></div>
    <div class="card"><div class="card-icon">🤖</div><div class="card-title">Research Agents</div>
      <div class="card-body">Autonomous Claude agents that receive a strategic question and find the answer — searching signals, web, and past dispatches autonomously.</div>
      <span class="card-tag tag-future">Vision</span></div>
    <div class="card"><div class="card-icon">🧠</div><div class="card-title">Agency Memory</div>
      <div class="card-body">Fine-tuned on past briefs and campaigns. The system learns what "good" looks like for this specific agency. Intelligence compounds with every use.</div>
      <span class="card-tag tag-future">Vision</span></div>
  </div>
</div>

<div class="panel" id="exp">
  <div class="eyebrow">Strategy UX</div>
  <div class="big-title">Tools strategists actually use</div>
  <div class="lead">The best intelligence is useless if it doesn't fit how strategists think and work.</div>
  <div class="grid grid-3">
    <div class="card"><div class="card-icon">📅</div><div class="card-title">Cultural Calendar</div>
      <div class="card-body">Upcoming cultural moments mapped against brand relevance. Know three weeks in advance which moments are worth owning.</div>
      <span class="card-tag tag-next">Next</span></div>
    <div class="card"><div class="card-icon">⏱️</div><div class="card-title">Window Detector</div>
      <div class="card-body">When cultural conditions align for brand action — the 10-day window when the conversation is at peak receptivity.</div>
      <span class="card-tag tag-next">Next</span></div>
    <div class="card"><div class="card-icon">🔔</div><div class="card-title">Alert Engine</div>
      <div class="card-body">Push to Slack, email, or SMS when thresholds are crossed. The system works while you sleep.</div>
      <span class="card-tag tag-next">Next</span></div>
    <div class="card"><div class="card-icon">💬</div><div class="card-title">Strategy Chat</div>
      <div class="card-body">"Ask the Lighthouse" — conversational interface to the entire signal database. Sourced, specific answers in seconds.</div>
      <span class="card-tag tag-future">6 months</span></div>
    <div class="card"><div class="card-icon">🎨</div><div class="card-title">Creative Springboard</div>
      <div class="card-body">From strategic direction to three creative territories with tone of voice, visual references, and platform strategy.</div>
      <span class="card-tag tag-future">6 months</span></div>
    <div class="card"><div class="card-icon">📊</div><div class="card-title">Client Mode</div>
      <div class="card-body">Polished client-facing view — no agency backstage visible. The Lighthouse becomes the deliverable, not the source.</div>
      <span class="card-tag tag-future">Vision</span></div>
  </div>
</div>

<div class="panel" id="road">
  <div class="eyebrow">Execution Roadmap</div>
  <div class="big-title">From prototype to platform</div>
  <div class="lead">A phased approach delivering client value at every stage — compounding intelligence, not a big-bang launch.</div>
  <div class="timeline">
    <div class="tl-item">
      <div class="tl-dot" style="background:#0fa3b5;"></div>
      <div class="tl-label">Now — v1 Complete</div>
      <div class="tl-title">Lighthouse Core</div>
      <div class="tl-body">Editorial dispatch · Signal Map · Momentum Tracker · Competitive Pulse · Raw Signal Feed · Briefing Builder · Signal Lab · Archive · PDF Export. Sellable today.</div>
    </div>
    <div class="tl-item">
      <div class="tl-dot" style="background:#0fa3b5;box-shadow:0 0 0 3px rgba(10,125,140,.3);"></div>
      <div class="tl-label">v2 · 1–2 months</div>
      <div class="tl-title">Intelligence Depth</div>
      <div class="tl-body">Exa.ai + GDELT + YouTube in pipeline · Sentiment layer · Anomaly alerts (Slack) · Cultural Calendar · Window Detector. Price: tier up.</div>
    </div>
    <div class="tl-item">
      <div class="tl-dot" style="background:#1a8a6b;"></div>
      <div class="tl-label">v3 · 3–4 months</div>
      <div class="tl-title">Strategy Suite</div>
      <div class="tl-body">Narrative clustering · Momentum forecasting · Strategy Chat · Creative Springboard · Client mode · White-label. Price: agency retainer.</div>
    </div>
    <div class="tl-item">
      <div class="tl-dot" style="background:#6ea8c4;"></div>
      <div class="tl-label">v4 · 6 months+</div>
      <div class="tl-title">Cultural Intelligence Platform</div>
      <div class="tl-body">Research agents · Agency memory · Cultural Tension Mapper · War Room Mode · API for research firms. Price: platform.</div>
    </div>
  </div>
</div>

<script>
function show(id,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}
</script>
</body></html>"""

st.components.v1.html(_VISION_MAP_HTML, height=550, scrolling=False)

# ── Sweep scheduling & velocity panel ─────────────────────────────────────────
st.markdown("""
<div style="border-top:2px solid #e4e2db;padding-top:2rem;margin-top:2.5rem;">
  <span style="font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.18em;
       text-transform:uppercase;color:#0a7d8c;font-weight:700;">◉ Path B + C</span>
  <div style="font-family:'Georgia',serif;font-size:22px;font-weight:600;
       color:#071828;margin:8px 0 4px;">Sweep Scheduling & Trend Velocity</div>
  <div style="font-family:'Georgia',serif;font-style:italic;font-size:13px;color:#274d68;
       margin-bottom:20px;">Configure automated sweep frequency and track how signal volume
       evolves over time per topic.</div>
</div>
""", unsafe_allow_html=True)

_sched_col, _vel_col = st.columns([1, 1], gap="large")

with _sched_col:
    st.markdown("##### ⏱ Sweep frequency")
    _sweep_freq = st.select_slider(
        "Run automatic sweeps",
        options=["Manual only", "Daily", "Every 2 days", "Weekly"],
        value=st.session_state.get("sweep_frequency", "Manual only"),
        key="sweep_frequency",
        label_visibility="collapsed",
    )
    st.caption(f"**Current setting:** {_sweep_freq}")

    if _sweep_freq != "Manual only":
        st.info(
            f"⚙ Automated sweeps are set to **{_sweep_freq}**. "
            "To run automatically, deploy this app and wire `run_sweep()` to a cron job or "
            "Streamlit Cloud's scheduled reruns. The **⚡ Sweep & Generate** button always works on demand.",
            icon="ℹ️",
        )
    else:
        st.caption("Use **⚡ Sweep & Generate** in the sidebar to run a sweep manually.")

    st.markdown("---")
    st.markdown("##### 📌 Log this sweep run to DB")
    st.caption("After running a sweep, press this to record it for velocity tracking.")
    _st_topic = st.text_input("Topic swept", placeholder="e.g. Cultural identity", key="vel_topic_input")
    _st_count = st.number_input("Signals found", min_value=0, value=0, step=1, key="vel_count_input")
    if st.button("📥 Record sweep run", use_container_width=True, key="record_sweep_btn"):
        if not _st_topic.strip():
            st.warning("Enter the topic name.")
        elif not _db.use_supabase():
            st.warning("Supabase not configured — can't persist sweep history.")
        else:
            _run_id = _db.record_sweep_run(
                topic=_st_topic.strip(),
                signal_count=int(_st_count),
                sources=["manual"],
            )
            if _run_id:
                st.toast(f"✓ Sweep run recorded (id: {_run_id[:8]}…)")
            else:
                st.warning("Recording failed — check Supabase connection.")

with _vel_col:
    st.markdown("##### 📈 Trend velocity")
    st.caption("How fast signal volume is changing per topic across sweeps.")

    if _db.use_supabase():
        _recent_runs = _db.load_sweep_runs(limit=30)
        if _recent_runs:
            # Group by topic → show count trend
            from collections import defaultdict
            _topic_counts: dict = defaultdict(list)
            for _r in reversed(_recent_runs):
                _topic_counts[_r.get("topic", "?")].append(_r.get("signal_count", 0))

            for _t, _cnts in list(_topic_counts.items())[:6]:
                _delta = _cnts[-1] - _cnts[0] if len(_cnts) > 1 else 0
                _arrow = "↑" if _delta > 0 else ("↓" if _delta < 0 else "→")
                _color = "#1a6b4a" if _delta > 0 else ("#9d2a2a" if _delta < 0 else "#6e6e6e")
                st.markdown(
                    f'<div style="margin-bottom:8px;padding:10px 14px;background:#f8f6f0;'
                    f'border-radius:8px;border-left:3px solid {_color}">'
                    f'<span style="font-weight:500;font-size:13px;color:#071828">{e(_t)}</span>'
                    f'<span style="float:right;font-family:monospace;font-size:13px;color:{_color}">'
                    f'{_arrow} {abs(_delta):+d} signals</span>'
                    f'<div style="font-size:11px;color:#6e6e6e;margin-top:3px">'
                    f'{len(_cnts)} sweep{"s" if len(_cnts)!=1 else ""} · '
                    f'latest: {_cnts[-1]} signals</div></div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("No sweep runs recorded yet. Run some sweeps and log them to see velocity trends here.")
    else:
        st.info("Connect Supabase to track trend velocity across sweeps.")

tab_roadmap.__exit__(None, None, None)


# ── Footer — absolute bottom of page ─────────────────────────────────────────

render_footer()
