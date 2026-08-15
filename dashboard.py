import base64
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

BASE_DIR = Path(__file__).parent

st.set_page_config(page_title="Water & Energy Guardian", page_icon="💧", layout="wide")


def inject_persistent_css(css_text, style_id):
    """Pasang <style> ke document induk lewat JS, sekali saja per sesi
    browser (dicek via id elemen). Ini menghindari flash-of-unstyled-content
    yang terjadi kalau CSS besar dibongkar-pasang ulang lewat st.markdown()
    di setiap rerun Streamlit — termasuk penyebab sidebar sempat "naik"
    menutupi header sesaat sebelum CSS layout-nya terpasang kembali."""
    escaped = css_text.replace("\\", "\\\\").replace("`", "\\`").replace("</script", "<\\/script")
    components.html(
        f"""
        <script>
        (function() {{
            var doc = window.parent.document;
            if (!doc.getElementById('{style_id}')) {{
                var style = doc.createElement('style');
                style.id = '{style_id}';
                style.innerHTML = `{escaped}`;
                doc.head.appendChild(style);
            }}
        }})();
        </script>
        """,
        height=0,
        width=0,
    )


@st.cache_data
def load_logo_base64():
    with open(BASE_DIR / "logo.png", "rb") as f:
        return base64.b64encode(f.read()).decode()


@st.cache_data
def load_watermark_base64():
    with open(BASE_DIR / "barelang.png", "rb") as f:
        return base64.b64encode(f.read()).decode()


@st.cache_data
def load_dashboard_json():
    with open(BASE_DIR / "dashboard_data.json", "r") as f:
        return json.load(f)


@st.cache_data
def load_reservoir_csv():
    df = pd.read_csv(BASE_DIR / "reservoir_data.csv")
    df["date"] = pd.to_datetime(df["date"])
    return df


try:
    _logo_base64 = load_logo_base64()
    _watermark_base64 = load_watermark_base64()
    dashboard_data = load_dashboard_json()
    reservoir_df = load_reservoir_csv()
except (FileNotFoundError, json.JSONDecodeError) as e:
    st.error(f"Gagal memuat data dashboard: {e}. Pastikan file logo.png, barelang.png, "
             f"dashboard_data.json, dan reservoir_data.csv ada di folder yang sama dengan dashboard.py.")
    st.stop()

_logo_src = f"data:image/png;base64,{_logo_base64}"
_watermark_src = f"data:image/png;base64,{_watermark_base64}"

# ---------------------------------------------------------------------------
# Turunan data reservoir (dipakai bareng oleh halaman Dashboard & Summary)
# ---------------------------------------------------------------------------
STATUS = dashboard_data["status"]
EARLY_WARNING = dashboard_data["early_warning"]
DC_BASELINE = dashboard_data["dc_baseline"]

KOTA_LEVEL = STATUS["kota_pct"]
KOTA_STATUS_LABEL = STATUS["kota_status"]
NONGSA_LEVEL = STATUS["nongsa_pct"]
NONGSA_TREND_POIN = abs(STATUS["nongsa_trend_8mo_poin"])
SIAGA_THRESHOLD = EARLY_WARNING["target_pct"]
SIAGA_LABEL = EARLY_WARNING["target_label"]
SIAGA_ETA_DAYS = EARLY_WARNING["eta_days"]
SIAGA_ETA_LABEL = EARLY_WARNING["eta_label"]

TODAY = pd.Timestamp(dashboard_data["last_updated"])


def split_hist_forecast(df, value_col, lower_col, upper_col):
    hist = df[~df["is_forecast"]][["date", value_col]].rename(
        columns={"date": "Tanggal", value_col: "Level (%)"}
    )
    hist["Tipe"] = "Historis"
    forecast = df[df["is_forecast"]][["date", value_col, lower_col, upper_col]].rename(
        columns={"date": "Tanggal", value_col: "Level (%)", lower_col: "Lower", upper_col: "Upper"}
    )
    forecast["Tipe"] = "Forecast"
    return hist, forecast


kota_hist, kota_forecast = split_hist_forecast(reservoir_df, "kota_pct", "kota_lower", "kota_upper")
nongsa_hist, nongsa_forecast = split_hist_forecast(reservoir_df, "nongsa_pct", "nongsa_lower", "nongsa_upper")

kota_df = pd.concat([kota_hist, kota_forecast], ignore_index=True)

# Sambungan ekstrapolasi linear Nongsa dari ujung forecast 90 hari (CSV) sampai
# titik target (Siaga) pada eta_days — sesuai method di dashboard_data.json.
_extra_days = SIAGA_ETA_DAYS - len(nongsa_forecast)
if _extra_days > 0:
    _ext_start_val = nongsa_forecast["Level (%)"].iloc[-1]
    _ext_dates = pd.date_range(start=nongsa_forecast["Tanggal"].max() + pd.Timedelta(days=1), periods=_extra_days)
    _ext_vals = [
        _ext_start_val + (SIAGA_THRESHOLD - _ext_start_val) * ((i + 1) / _extra_days)
        for i in range(_extra_days)
    ]
    nongsa_forecast_full = pd.concat([
        nongsa_forecast,
        pd.DataFrame({"Tanggal": _ext_dates, "Level (%)": _ext_vals, "Tipe": "Forecast"}),
    ], ignore_index=True)
else:
    nongsa_forecast_full = nongsa_forecast

nongsa_df = pd.concat([nongsa_hist, nongsa_forecast_full], ignore_index=True)


def trend_chart(df, y_domain, forecast_color):
    return alt.Chart(df).mark_line(strokeWidth=2.5).encode(
        x=alt.X("Tanggal:T", title=None),
        y=alt.Y("Level (%):Q", title="Level Reservoir (%)", scale=alt.Scale(domain=y_domain)),
        color=alt.Color(
            "Tipe:N", title=None,
            scale=alt.Scale(domain=["Historis", "Forecast"], range=["#1ca8d8", forecast_color]),
            legend=alt.Legend(orient="top"),
        ),
        strokeDash=alt.StrokeDash(
            "Tipe:N",
            scale=alt.Scale(domain=["Historis", "Forecast"], range=[[1, 0], [6, 4]]),
            legend=None,
        ),
    )


def confidence_band(forecast_df, color):
    return alt.Chart(forecast_df).mark_area(opacity=0.15, color=color).encode(
        x=alt.X("Tanggal:T"), y=alt.Y("Lower:Q"), y2=alt.Y2("Upper:Q"),
    )


# --- Alerts di-generate otomatis dari ambang batas kota_pct/nongsa_pct ---
ALERT_TIERS = [(85, "warning", "WATCH"), (70, "warning", "WARNING"), (50, "critical", "SIAGA")]


def generate_threshold_alerts(df, column, reservoir_name):
    alerts, reached_tier = [], -1
    for _, row in df.sort_values("date").iterrows():
        val = row[column]
        crossed_idx = max([i for i, (t, _, _) in enumerate(ALERT_TIERS) if val < t], default=-1)
        if crossed_idx > reached_tier:
            threshold, level, label = ALERT_TIERS[crossed_idx]
            alerts.append({
                "tanggal": row["date"],
                "level": level,
                "pesan": f"{reservoir_name} turun di bawah {threshold}% (level {label}) — saat ini {val:.1f}%.",
            })
            reached_tier = crossed_idx
    return alerts


_alerts_list = (
    generate_threshold_alerts(reservoir_df, "kota_pct", "Reservoir Kota")
    + generate_threshold_alerts(reservoir_df, "nongsa_pct", "Sei Nongsa")
    + [{
        "tanggal": TODAY + pd.Timedelta(days=SIAGA_ETA_DAYS),
        "level": "critical",
        "pesan": f"Proyeksi: Sei Nongsa diperkirakan mencapai zona {SIAGA_LABEL} ({SIAGA_THRESHOLD}%) "
                 f"dalam {SIAGA_ETA_LABEL} ({EARLY_WARNING['method']}).",
    }]
)
alerts_df = pd.DataFrame(_alerts_list)
alerts_df["tanggal"] = pd.to_datetime(alerts_df["tanggal"])

if "sidebar_collapsed" not in st.session_state:
    st.session_state.sidebar_collapsed = False
if "menu" not in st.session_state:
    st.session_state.menu = "Dashboard"
if "alerts_checked" not in st.session_state:
    st.session_state.alerts_checked = {}
if "alerts_dismissed" not in st.session_state:
    st.session_state.alerts_dismissed = {}

# ---------------------------------------------------------------------------
# Global CSS (header, sidebar, cards) — dipasang sekali, dipakai di semua halaman
# ---------------------------------------------------------------------------
global_css = """
<style>
header[data-testid="stHeader"] {
    display: none;
}

.header {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    width: 100%;
    z-index: 999999;
    box-sizing: border-box;
    background-color: #14182b;
    padding: 18px 32px;
    color: #ffffff;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-family: Arial, sans-serif;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.25);
}

section[data-testid="stSidebar"] {
    padding-top: 88px !important;
    background-color: #181d33 !important;
    transition: width 0.2s ease-in-out;
    /* Potong fisik 88px teratas dari kotak sidebar itu sendiri, supaya
       area yang ditempati header tidak PERNAH tergambar apa pun dari
       sidebar — terlepas dari isu timing/urutan z-index saat rerun. */
    clip-path: inset(88px 0 0 0);
}
section[data-testid="stSidebar"] > div {
    background-color: #181d33 !important;
}

[data-testid="stAppViewContainer"] .block-container {
    padding-top: 96px !important;
}

[data-testid="stAppViewContainer"] > .main,
[data-testid="stMain"] {
    background-image:
        linear-gradient(rgba(255, 255, 255, 0.93), rgba(255, 255, 255, 0.93)),
        url("%%WATERMARK_SRC%%");
    background-repeat: no-repeat;
    background-position: center center;
    background-size: 45vw;
    background-attachment: fixed;
}

.header-left-wrap {
    display: flex;
    align-items: center;
    gap: 18px;
}
.header-menu-icon-spacer {
    width: 52px;
    flex-shrink: 0;
}
.header-logo-box {
    background: #ffffff;
    border-radius: 12px;
    padding: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
}
.header-logo {
    width: 40px;
    height: 40px;
    object-fit: contain;
    display: block;
}
.header-left {
    font-size: 26px;
    font-weight: bold;
    color: #ffffff;
    letter-spacing: 0.2px;
}

.st-key-sidebar_toggle_btn {
    position: fixed !important;
    top: 18px !important;
    left: 32px !important;
    z-index: 1000001 !important;
    width: 52px !important;
    height: 52px !important;
}
.st-key-sidebar_toggle_btn button {
    width: 52px !important;
    height: 52px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    background: transparent !important;
    border: none !important;
    border-radius: 12px !important;
    box-shadow: none !important;
    color: #ffffff !important;
    font-size: 50px !important;
    line-height: 1 !important;
    padding: 0 !important;
    min-height: unset !important;
}
.st-key-sidebar_toggle_btn button p,
.st-key-sidebar_toggle_btn button span {
    font-size: 42px !important;
    line-height: 1 !important;
}
.st-key-sidebar_toggle_btn button:hover {
    background: rgba(255, 255, 255, 0.12) !important;
}

/* Tombol navigasi sidebar */
section[data-testid="stSidebar"] div.stButton > button {
    font-size: 18px !important;
    line-height: 1.2 !important;
    font-weight: 600 !important;
    height: 52px !important;
    margin-top: 4px !important;
    padding: 10px 18px !important;
    text-align: left !important;
    justify-content: flex-start !important;
    border-radius: 10px !important;
    transition: all 0.2s ease !important;
}
section[data-testid="stSidebar"] div.stButton > button p,
section[data-testid="stSidebar"] div.stButton > button span {
    font-size: 18px !important;
    font-weight: 600 !important;
    text-align: left !important;
    justify-content: flex-start !important;
}
section[data-testid="stSidebar"] div.stButton > button:hover {
    transform: translateX(2px);
}
section[data-testid="stSidebar"] div.stButton > button[kind="secondary"] {
    background-color: transparent !important;
    border: none !important;
    border-left: 4px solid transparent !important;
    color: #cbd5e1 !important;
}
section[data-testid="stSidebar"] div.stButton > button[kind="secondary"] p,
section[data-testid="stSidebar"] div.stButton > button[kind="secondary"] span {
    color: #cbd5e1 !important;
}
section[data-testid="stSidebar"] div.stButton > button[kind="secondary"]:hover {
    background-color: rgba(255, 255, 255, 0.06) !important;
    color: #ffffff !important;
}
section[data-testid="stSidebar"] div.stButton > button[kind="secondary"]:hover p,
section[data-testid="stSidebar"] div.stButton > button[kind="secondary"]:hover span {
    color: #ffffff !important;
}

/* Checkbox custom (dipakai di halaman Alerts) */
input[type="checkbox"] {
    width: 36px !important;
    height: 36px !important;
    min-width: 36px !important;
    cursor: pointer !important;
    accent-color: #00d4ff !important;
    transition: all 0.3s ease !important;
}
input[type="checkbox"]:hover {
    transform: scale(1.1) !important;
    filter: brightness(1.2) !important;
}
input[type="checkbox"]:checked {
    accent-color: #22c55e !important;
}
div:has(> input[type="checkbox"]) {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 12px 16px !important;
    background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%) !important;
    border: 2px solid #cbd5e1 !important;
    border-radius: 12px !important;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08) !important;
    transition: all 0.3s ease !important;
    margin-right: 4px !important;
}
div:has(> input[type="checkbox"]):hover {
    border-color: #00d4ff !important;
    box-shadow: 0 4px 12px rgba(0, 212, 255, 0.15) !important;
    background: linear-gradient(135deg, #e0f7ff 0%, #cce7ff 100%) !important;
}

/* Kartu gauge & status (halaman Dashboard) — ukuran & padding disamakan agar grid rapi */
.water-gauge-card, .power-gauge-card, .status-card {
    height: 220px !important;
    min-height: 0 !important;
    max-height: none !important;
    box-sizing: border-box !important;
    border-radius: 18px;
    padding: 20px 18px;
    overflow: hidden;
}
.water-gauge-card, .power-gauge-card {
    /* Block layout biasa (bukan flex) — supaya judul selalu mulai persis di
       padding-top, sama seperti .status-card, tanpa distorsi dari flexbox. */
    text-align: center;
}
.water-gauge-card {
    background: linear-gradient(180deg, #f7fbfe 0%, #eef8fc 100%);
    border: 1px solid #dfeef3;
    box-shadow: 0 8px 18px rgba(17, 76, 110, 0.08);
}
.power-gauge-card {
    background: linear-gradient(180deg, #fffdf7 0%, #fff7ee 100%);
    border: 1px solid #f0e2d0;
    box-shadow: 0 8px 18px rgba(124, 85, 22, 0.08);
}
.water-gauge-title, .power-gauge-title {
    font-size: 18px;
    font-weight: 700;
    line-height: 1.2;
    margin: 0 0 12px;
    text-align: center;
}
.water-gauge-title { color: #0b3d59; }
.power-gauge-title { color: #5c3d1a; }

.gauge-wrap, .power-gauge-wrap {
    position: relative;
    width: 150px;
    height: 150px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: center;
}
.gauge-svg, .power-gauge-svg {
    width: 150px;
    height: 150px;
    max-width: 100%;
    overflow: visible;
    transform: rotate(-90deg);
}
.gauge-bg { fill: none; stroke: #dfeaf0; stroke-width: 18; }
.power-gauge-bg { fill: none; stroke: #f2e1c8; stroke-width: 18; }
.gauge-progress { fill: none; stroke: url(#waterGradient); stroke-width: 18; stroke-linecap: round; }
.power-gauge-progress { fill: none; stroke: url(#powerGradient); stroke-width: 18; stroke-linecap: round; }

.gauge-value, .power-gauge-value {
    position: absolute;
    inset: 14px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    font-weight: 700;
}
.gauge-value { color: #123d5f; }
.power-gauge-value { color: #5d3b14; }
.gauge-value .value-main { font-size: 38px; line-height: 1; }
.power-gauge-value .value-main { font-size: 30px; line-height: 1; }
.gauge-value .value-scale { font-size: 14px; color: #5d7380; margin-top: 4px; }
.power-gauge-value .value-scale { font-size: 13px; color: #8a6b4d; margin-top: 4px; }

.status-card {
    /* Block layout biasa (bukan flex) — konsisten dengan gauge card di atas. */
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.06);
    border: 1px solid #e5e7eb;
}
.status-card.alerts-status {
    background: linear-gradient(180deg, #fff7f7 0%, #fff1f2 100%);
    border-color: #f7d5d8;
}
.status-card.health-card {
    background: linear-gradient(180deg, #f2fff8 0%, #eafaf1 100%);
    border-color: #cfeedd;
}
.status-card.watch-card {
    background: linear-gradient(180deg, #fffaf3 0%, #fff5e6 100%);
    border-color: #f7d9a3;
}
.status-card.warning-card {
    background: linear-gradient(180deg, #fffaf3 0%, #fff5e6 100%);
    border-color: #f7d9a3;
    border-left: 5px solid #f59e0b;
}
.status-card.warning-card .status-meta {
    font-size: 14.5px;
    line-height: 1.55;
    color: #7c4a03;
    font-weight: 500;
}
.status-label {
    font-size: 18px;
    font-weight: 700;
    line-height: 1.2;
    color: #475569;
    margin: 0 0 12px;
    letter-spacing: 0.2px;
}
.status-value {
    font-size: 34px;
    font-weight: 800;
    color: #1f2937;
    line-height: 1.1;
    margin-bottom: 8px;
}
.status-meta {
    font-size: 14px;
    color: #64748b;
    font-weight: 600;
    margin-bottom: 12px;
}
.status-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: fit-content;
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.2px;
}
.status-pill.alert-pill { background: #fde7e7; color: #b42318; }
.status-pill.health-pill { background: #dcfce7; color: #166534; }
.status-pill.watch-pill { background: #fef3c7; color: #92400e; }

/* Kartu grafik — dipasang lewat st.container(key=...), bukan div markdown
   biasa, supaya chart Streamlit sungguhan berada di DALAM kartu (bukan
   sekadar bersebelahan dengannya). */
.st-key-water_chart_card,
.st-key-summary_chart_card,
.st-key-kota_chart_card,
.st-key-nongsa_chart_card {
    background: linear-gradient(180deg, #ffffff 0%, #f8fbfe 100%);
    border: 1px solid #dfeaf2;
    border-radius: 18px;
    padding: 20px 20px 24px;
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.04);
    margin-top: 16px;
    margin-bottom: 20px;
}

.metric-card {
    background: linear-gradient(180deg, #ffffff 0%, #f8fbfe 100%);
    border: 1px solid #dfeaf2;
    border-radius: 18px;
    padding: 18px 16px;
    height: 196px;
    box-sizing: border-box;
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.04);
    display: flex;
    flex-direction: column;
    justify-content: center;
}
.metric-card .metric-label {
    font-size: 15px;
    font-weight: 700;
    color: #475569;
    margin-bottom: 12px;
    letter-spacing: 0.2px;
}
.metric-card .metric-value {
    font-size: 34px;
    font-weight: 800;
    line-height: 1.1;
    color: #0f172a;
    margin-bottom: 8px;
    overflow-wrap: anywhere;
}
.metric-card .metric-detail { font-size: 13px; color: #64748b; font-weight: 600; }
.metric-card.pue-card { background: linear-gradient(180deg, #f0f9ff 0%, #edf7ff 100%); border-color: #cfeaf9; }
.metric-card.wue-card { background: linear-gradient(180deg, #f4fff9 0%, #edfdf4 100%); border-color: #d6f4e1; }

/* Kartu alert (halaman Alerts) */
.alert-card {
    background: linear-gradient(180deg, #fffaf3 0%, #fff5e6 100%);
    border: 1px solid #f7c977;
    border-left: 5px solid #f59e0b;
    border-radius: 12px;
    padding: 14px 16px 12px;
    margin-top: 12px;
    margin-bottom: 10px;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
    overflow: hidden;
    word-wrap: break-word;
}
.alert-card.critical {
    background: linear-gradient(180deg, #fffaf8 0%, #fff0f0 100%);
    border-color: #f3b1ad;
    border-left-color: #ef4444;
}
.alert-card.checked {
    background: linear-gradient(180deg, #f6fff9 0%, #edfdf4 100%);
    border-color: #bfe7cf;
    border-left-color: #22c55e;
    opacity: 0.92;
}
.alert-date {
    font-size: 12px;
    font-weight: 700;
    color: #64748b;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 8px;
}
.alert-message { color: #1f2937; font-size: 16px; font-weight: 500; line-height: 1.45; }
.alert-tag {
    display: inline-block;
    margin-top: 10px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.08em;
}
.alert-tag.warning { background: #fef3c7; color: #92400e; }
.alert-tag.critical { background: #fee2e2; color: #b91c1c; }

/* Kartu ringkasan (halaman Summary) */
.summary-header {
    margin: 0 0 16px;
    color: #0f172a;
    font-size: 2rem;
    font-weight: 700;
}
.summary-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 18px;
    margin-top: 8px;
    margin-bottom: 20px;
}
.summary-card {
    min-height: 170px;
    border-radius: 18px;
    padding: 18px 16px 14px;
    box-sizing: border-box;
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.06);
    border: 1px solid #dfeaf2;
    background: linear-gradient(180deg, #ffffff 0%, #f8fbfe 100%);
}
.summary-card.blue { background: linear-gradient(180deg, #f0f9ff 0%, #edf7ff 100%); border-color: #cfeaf9; }
.summary-card.green { background: linear-gradient(180deg, #f1fff8 0%, #ebfaf2 100%); border-color: #d0f0df; }
.summary-card.orange { background: linear-gradient(180deg, #fffaf1 0%, #fff6e8 100%); border-color: #fde4ba; }
.summary-label {
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #64748b;
    margin-bottom: 12px;
}
.summary-value { font-size: 32px; font-weight: 800; color: #0f172a; line-height: 1.1; margin-bottom: 8px; }
.summary-meta { font-size: 13px; color: #475569; font-weight: 600; }
@media (max-width: 900px) {
    .summary-grid { grid-template-columns: 1fr; }
}

/* Panel Simulasi Data Center (halaman Summary) */
.st-key-dc_sim_card {
    background: linear-gradient(180deg, #ffffff 0%, #f8fbfe 100%);
    border: 1px solid #dfeaf2;
    border-radius: 18px;
    padding: 22px 24px;
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.04);
    margin-top: 16px;
}
.dc-sim-title {
    font-size: 17px;
    font-weight: 700;
    color: #0f172a;
}
.dc-sim-hint {
    font-size: 12px;
    color: #94a3b8;
    font-style: italic;
}
.dc-sim-slider-label {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #64748b;
    margin-top: 6px;
}
.mini-stat-card {
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 12px 14px;
    height: 100%;
    box-sizing: border-box;
}
.mini-stat-card .mini-label {
    font-size: 10.5px;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #64748b;
    margin-bottom: 8px;
}
.mini-stat-card .mini-value {
    font-size: 21px;
    font-weight: 800;
    color: #0f172a;
}
.mini-stat-card .mini-value .unit {
    font-size: 12px;
    font-weight: 600;
    color: #64748b;
    margin-left: 3px;
}
.energy-bar-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 6px 0 4px;
    font-size: 13px;
    font-weight: 600;
    color: #475569;
}
.energy-bar-wrap {
    width: 100%;
    height: 10px;
    background: #e2e8f0;
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 18px;
}
.energy-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #14532d, #f59e0b);
    border-radius: 999px;
}
.dc-impact-caption {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #94a3b8;
    border-top: 1px solid #e2e8f0;
    padding-top: 16px;
    margin-bottom: 14px;
}
.dc-impact-value {
    font-size: 26px;
    font-weight: 800;
    color: #0f172a;
    line-height: 1.1;
}
.dc-impact-label {
    font-size: 12.5px;
    color: #64748b;
    font-weight: 600;
    margin-top: 4px;
}

/* Badge panah + efek kedip saat nilai simulasi berubah */
@keyframes dcValuePulse {
    0% { background-color: rgba(0, 212, 255, 0.28); }
    100% { background-color: rgba(0, 212, 255, 0); }
}
.dc-value-pulse {
    display: inline-block;
    border-radius: 6px;
    padding: 0 4px;
    margin: 0 -4px;
    animation: dcValuePulse 0.8s ease-out;
    transition: color 0.3s ease;
}
.dc-value-pulse.value-good { color: #16a34a; }
.dc-value-pulse.value-bad { color: #dc2626; }
@keyframes dcBadgePop {
    0% { opacity: 0; transform: scale(0.6) translateY(2px); }
    60% { opacity: 1; transform: scale(1.08) translateY(0); }
    100% { opacity: 1; transform: scale(1) translateY(0); }
}
.dc-delta-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 13px;
    font-weight: 800;
    margin-left: 8px;
    padding: 2px 9px;
    border-radius: 999px;
    vertical-align: middle;
    animation: dcBadgePop 0.4s ease-out;
}
.dc-delta-badge.delta-good { background: #dcfce7; color: #166534; }
.dc-delta-badge.delta-bad { background: #fee2e2; color: #b91c1c; }
.action-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}
.action-chip {
    display: inline-flex;
    align-items: center;
    padding: 9px 16px;
    border-radius: 999px;
    background: #f1f5f9;
    color: #334155;
    font-size: 13px;
    font-weight: 600;
    border: 1px solid #e2e8f0;
}
.data-notes-card {
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 16px 20px;
    margin-top: 16px;
}
.data-notes-card .data-notes-title {
    font-size: 13px;
    font-weight: 700;
    color: #475569;
    margin-bottom: 10px;
}
.data-notes-card .data-notes-row {
    font-size: 12.5px;
    color: #64748b;
    line-height: 1.7;
}
.data-notes-card .data-notes-row b { color: #334155; }

/* Footer */
.app-footer {
    width: 100%;
    text-align: right;
    font-size: 16px;
    font-weight: 600;
    color: #475569;
    letter-spacing: 0.2px;
    margin-top: 56px;
    padding-top: 18px;
    border-top: 1px solid #e2e8f0;
    font-family: Arial, sans-serif;
}

/* Responsif untuk layar sempit */
@media (max-width: 640px) {
    .water-gauge-card, .power-gauge-card, .status-card {
        height: auto;
        min-height: 200px;
    }
    .header-left {
        font-size: 18px;
    }
}
</style>
"""

_global_css_text = (
    global_css.replace("%%WATERMARK_SRC%%", _watermark_src)
    .replace("<style>", "")
    .replace("</style>", "")
)
inject_persistent_css(_global_css_text, "app-global-css")

sidebar_collapsed_css = """
<style>
section[data-testid="stSidebar"] {
    width: 100px !important;
    min-width: 100px !important;
    max-width: 100px !important;
    overflow: hidden !important;
    padding: 0 !important;
    border: none !important;
}
section[data-testid="stSidebar"] div.stButton {
    opacity: 0 !important;
    pointer-events: none !important;
}
</style>
""" if st.session_state.sidebar_collapsed else ""

if sidebar_collapsed_css:
    st.markdown(sidebar_collapsed_css, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.markdown(
    f"""
    <div class="header">
        <div class="header-left-wrap">
            <div class="header-menu-icon-spacer"></div>
            <div class="header-logo-box">
                <img src="{_logo_src}" class="header-logo" alt="logo">
            </div>
            <div class="header-left">Water & Energy Guardian · Data Center Resource Monitoring</div>
        </div>
        <div class="header-right"></div>
    </div>
    """,
    unsafe_allow_html=True,
)

if st.button("☰", key="sidebar_toggle_btn"):
    st.session_state.sidebar_collapsed = not st.session_state.sidebar_collapsed
    st.rerun()

# ---------------------------------------------------------------------------
# Sidebar navigasi
# ---------------------------------------------------------------------------
# Catatan: TIDAK pakai st.rerun() di sini. Streamlit sudah otomatis rerun satu
# kali setiap tombol diklik — memanggil st.rerun() lagi di dalamnya memaksa
# rerun KEDUA yang membatalkan render pertama di tengah jalan (termasuk header
# yang sudah sempat digambar), itulah yang terlihat sebagai "kedip" tadi.
# Highlight tombol aktif juga sengaja dipindah ke CSS (bukan parameter type=
# saat pembuatan tombol), supaya tetap akurat walau tanpa rerun kedua.
_clicked_page = None
for page in ["Dashboard", "Alerts", "Summary"]:
    if st.sidebar.button(page, key=f"nav_{page}", use_container_width=True):
        _clicked_page = page

if _clicked_page:
    st.session_state.menu = _clicked_page
menu = st.session_state.menu

st.markdown(
    f"""
    <style>
    .st-key-nav_{menu} button {{
        background-color: rgba(0, 212, 255, 0.14) !important;
        border: none !important;
        border-left: 4px solid #00d4ff !important;
        color: #00d4ff !important;
        border-radius: 0 10px 10px 0 !important;
    }}
    .st-key-nav_{menu} button p,
    .st-key-nav_{menu} button span {{
        color: #00d4ff !important;
    }}
    .st-key-nav_{menu} button:hover {{
        background-color: rgba(0, 212, 255, 0.22) !important;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Halaman: Dashboard
# ---------------------------------------------------------------------------
if menu == "Dashboard":
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(
            f"""
            <div class="status-card health-card">
                <div class="status-label">Kota (weighted, 7 waduk)</div>
                <div class="status-value">{KOTA_LEVEL:.0f}%</div>
                <div class="status-meta">Status reservoir gabungan kota</div>
                <div class="status-pill health-pill">{KOTA_STATUS_LABEL}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col2:
        st.markdown(
            f"""
            <div class="status-card watch-card">
                <div class="status-label">Sei Nongsa</div>
                <div class="status-value">{NONGSA_LEVEL:.0f}%</div>
                <div class="status-meta">↓ {NONGSA_TREND_POIN:.1f} poin dalam 8 bulan terakhir</div>
                <div class="status-pill watch-pill">Menurun</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col3:
        st.markdown(
            f"""
            <div class="status-card warning-card">
                <div class="status-label">⚠ Early Warning</div>
                <div class="status-meta">Nongsa diproyeksi capai zona {SIAGA_LABEL} ({SIAGA_THRESHOLD}%) dalam
                {SIAGA_ETA_LABEL}. Ekstrapolasi tren linear forecast — cukup waktu untuk
                mitigasi jika dipantau sekarang.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown('<h3 style="margin: 1.5rem 0 0.75rem; color: #0f172a; font-size: 1.2rem;">Reservoir Kota — Historis & Forecast 90 Hari</h3>', unsafe_allow_html=True)
    with st.container(key="kota_chart_card"):
        kota_chart = (
            confidence_band(kota_forecast, "#f59e0b") + trend_chart(kota_df, [80, 105], "#f59e0b")
        ).properties(height=340)
        st.altair_chart(kota_chart, use_container_width=True)

    st.markdown('<h3 style="margin: 1.5rem 0 0.75rem; color: #0f172a; font-size: 1.2rem;">Sei Nongsa — Tren Menurun & Proyeksi</h3>', unsafe_allow_html=True)
    with st.container(key="nongsa_chart_card"):
        siaga_rule = alt.Chart(pd.DataFrame({"y": [SIAGA_THRESHOLD]})).mark_rule(
            color="#ef4444", strokeDash=[5, 4], size=2
        ).encode(y="y:Q")
        siaga_label = alt.Chart(
            pd.DataFrame({"y": [SIAGA_THRESHOLD], "label": [f"Ambang {SIAGA_LABEL} ({SIAGA_THRESHOLD}%)"]})
        ).mark_text(align="left", dx=4, dy=-8, color="#ef4444", fontWeight="bold", fontSize=11).encode(
            y="y:Q", text="label:N"
        )
        nongsa_chart = (
            confidence_band(nongsa_forecast, "#ef4444")
            + trend_chart(nongsa_df, [25, 105], "#ef4444")
            + siaga_rule + siaga_label
        ).properties(height=340)
        st.altair_chart(nongsa_chart, use_container_width=True)

# ---------------------------------------------------------------------------
# Halaman: Alerts
# ---------------------------------------------------------------------------
elif menu == "Alerts":
    col_start, col_end = st.columns(2)
    with col_start:
        start_date = st.date_input("Start Date", alerts_df["tanggal"].min())
    with col_end:
        end_date = st.date_input("End Date", alerts_df["tanggal"].max())

    date_range_alerts = alerts_df[
        (alerts_df["tanggal"] >= pd.to_datetime(start_date))
        & (alerts_df["tanggal"] <= pd.to_datetime(end_date))
    ]
    filtered_alerts = date_range_alerts[
        ~date_range_alerts["tanggal"].dt.strftime("%Y-%m-%d").isin(st.session_state.alerts_dismissed.keys())
    ]

    st.markdown('<h3 style="margin: 0 0 8px; color: #0f172a;">Current Alerts</h3>', unsafe_allow_html=True)

    def toggle_alert(key):
        st.session_state.alerts_checked[key] = not st.session_state.alerts_checked.get(key, False)

    if filtered_alerts.empty:
        st.info("Tidak ada alert pada rentang tanggal yang dipilih.")
    else:
        for idx, (_, row) in enumerate(filtered_alerts.iterrows()):
            alert_key = row["tanggal"].strftime("%Y-%m-%d")
            is_checked = st.session_state.alerts_checked.get(alert_key, False)
            level = row["level"]
            status_label = "CHECKED" if is_checked else level.upper()
            card_class = "alert-card checked" if is_checked else f"alert-card {level}"
            tag_class = "critical" if is_checked else level

            col_card, col_checkbox = st.columns([0.92, 0.08])
            with col_card:
                st.markdown(
                    f"""
                    <div class="{card_class}">
                        <div class="alert-date">{row['tanggal'].strftime('%d %b %Y')}</div>
                        <div class="alert-message">{row['pesan']}</div>
                        <div class="alert-tag {tag_class}">{status_label}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with col_checkbox:
                st.checkbox("Tandai sudah diperiksa", value=is_checked, key=f"check_{alert_key}_{idx}",
                             on_change=toggle_alert, args=(alert_key,), label_visibility="collapsed")

    st.markdown('<div style="margin-top: 18px;">', unsafe_allow_html=True)
    button_col1, button_col2, button_col3 = st.columns(3)
    with button_col1:
        if st.button("✓ Mark All as Checked", key="mark_all_alerts_checked", use_container_width=True):
            for _, row in filtered_alerts.iterrows():
                st.session_state.alerts_checked[row["tanggal"].strftime("%Y-%m-%d")] = True
            st.rerun()
    with button_col2:
        if st.button("✗ Clear All", key="clear_all_alerts_checked", use_container_width=True):
            for _, row in date_range_alerts.iterrows():
                st.session_state.alerts_dismissed[row["tanggal"].strftime("%Y-%m-%d")] = True
            st.rerun()
    with button_col3:
        if st.button("↻ Restore All", key="restore_all_alerts", use_container_width=True):
            for _, row in date_range_alerts.iterrows():
                st.session_state.alerts_dismissed.pop(row["tanggal"].strftime("%Y-%m-%d"), None)
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Halaman: Summary
# ---------------------------------------------------------------------------
elif menu == "Summary":
    st.markdown(
        f"""
        <div class="summary-grid">
            <div class="summary-card blue">
                <div class="summary-label">Proyeksi Hari ke {SIAGA_LABEL}</div>
                <div class="summary-value">{SIAGA_ETA_DAYS}</div>
                <div class="summary-meta">hari ({SIAGA_ETA_LABEL}) — Sei Nongsa</div>
            </div>
            <div class="summary-card green">
                <div class="summary-label">Reservoir Efficiency Score</div>
                <div class="summary-value">{KOTA_LEVEL:.0f}</div>
                <div class="summary-meta">out of 100 (Kota, weighted 7 waduk)</div>
            </div>

        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<h3 style="margin: 1.5rem 0 0.75rem; color: #0f172a; font-size: 1.2rem;">Tren Reservoir Kota & Sei Nongsa</h3>', unsafe_allow_html=True)
    with st.container(key="summary_chart_card"):
        combined_df = pd.concat([
            kota_df.assign(Reservoir="Kota"),
            nongsa_df.assign(Reservoir="Nongsa"),
        ], ignore_index=True)
        summary_chart = alt.Chart(combined_df).mark_line(strokeWidth=2.5).encode(
            x=alt.X("Tanggal:T", title=None),
            y=alt.Y("Level (%):Q", title="Level Reservoir (%)"),
            color=alt.Color(
                "Reservoir:N", title=None,
                scale=alt.Scale(domain=["Kota", "Nongsa"], range=["#1ca8d8", "#f59e0b"]),
                legend=alt.Legend(orient="top"),
            ),
            strokeDash=alt.StrokeDash(
                "Tipe:N",
                scale=alt.Scale(domain=["Historis", "Forecast"], range=[[1, 0], [6, 4]]),
                legend=None,
            ),
        ).properties(height=340)
        st.altair_chart(summary_chart, use_container_width=True)

    st.markdown('<div style="margin-top: 18px;">', unsafe_allow_html=True)
    st.download_button(
        label="Download Data Reservoir (CSV)",
        data=reservoir_df.to_csv(index=False),
        file_name="reservoir_data_export.csv",
        mime="text/csv",
    )
    st.markdown('</div>', unsafe_allow_html=True)

    # --- Simulasi Data Center (interaktif) ---
    with st.container(key="dc_sim_card"):
        title_col, hint_col = st.columns([3, 2])
        with title_col:
            st.markdown('<div class="dc-sim-title">Simulasi Data Center</div>', unsafe_allow_html=True)
        with hint_col:
            st.markdown(
                '<div class="dc-sim-hint" style="text-align:right;">Geser → angka &amp; trade-off ikut berubah</div>',
                unsafe_allow_html=True,
            )

        slider_col, metrics_col = st.columns([1, 2.2])
        with slider_col:
            sim_servers = st.slider("Server", min_value=1000, max_value=20000,
                                     value=int(DC_BASELINE["servers"]), step=500)
            sim_wue = st.slider("WUE (L/kWh)", min_value=0.5, max_value=5.0,
                                 value=float(DC_BASELINE["wue_L_per_kWh"]), step=0.1)
            sim_recycle = st.slider("Daur ulang (%)", min_value=0, max_value=100,
                                     value=int(DC_BASELINE["recycle_rate_pct"]), step=5)

        # Perhitungan ulang otomatis — Streamlit rerun setiap slider digeser.
        power_per_server_kW = DC_BASELINE["power_per_server_kW"]
        energi_recycle_per_m3 = DC_BASELINE["energi_recycle_kWh_per_m3"]

        sim_energi_server = sim_servers * power_per_server_kW * 24          # kWh/hari
        sim_gross = sim_energi_server * sim_wue / 1000                      # m3/hari
        sim_recycled_volume = sim_gross * (sim_recycle / 100)               # m3/hari
        sim_dari_reservoir = sim_gross - sim_recycled_volume                # m3/hari
        sim_energi_recycle = sim_recycled_volume * energi_recycle_per_m3    # kWh/hari
        sim_total_energi = sim_energi_server + sim_energi_recycle           # kWh/hari
        recycle_energy_share = (sim_energi_recycle / sim_total_energi * 100) if sim_total_energi else 0
        nongsa_impact_pct = (
            sim_dari_reservoir / DC_BASELINE["nongsa_volume_m3"] * 100
            if DC_BASELINE["nongsa_volume_m3"] else 0
        )

        with metrics_col:
            stat_cols = st.columns(4)
            for col, label, value, unit in [
                (stat_cols[0], "Gross", f"{sim_gross:,.1f}", "m³/hari"),
                (stat_cols[1], "Dari Reservoir", f"{sim_dari_reservoir:,.1f}", "m³/hari"),
                (stat_cols[2], "Energi Server", f"{sim_energi_server:,.0f}", "kWh/hari"),
                (stat_cols[3], "Energi Recycle", f"{sim_energi_recycle:,.0f}", "kWh/hari"),
            ]:
                with col:
                    st.markdown(
                        f"""
                        <div class="mini-stat-card">
                            <div class="mini-label">{label}</div>
                            <div class="mini-value">{value}<span class="unit">{unit}</span></div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

        st.markdown(
            f"""
            <div class="energy-bar-row">
                <span>Total energi: {sim_total_energi:,.0f} kWh/hari</span>
                <span>Recycle = {recycle_energy_share:.1f}% dari energi DC</span>
            </div>
            <div class="energy-bar-wrap">
                <div class="energy-bar-fill" style="width: {min(recycle_energy_share, 100):.2f}%;"></div>
            </div>
            <div class="dc-impact-caption">
                Dampak Lingkungan — vs baseline {sim_servers:,} server / WUE {sim_wue:.1f} / recycle {sim_recycle}%
            </div>
            """,
            unsafe_allow_html=True,
        )

        def render_impact_metric(container, session_key, value, value_fmt, label, higher_is_better):
            """Render satu angka Dampak Lingkungan + badge panah (naik/turun)
            dan efek kedip singkat, dibandingkan dengan nilai render sebelumnya
            (disimpan di session_state) — jadi terlihat jelas saat slider diubah."""
            prev_value = st.session_state.get(session_key)
            delta = None if prev_value is None else value - prev_value
            st.session_state[session_key] = value

            badge_html, value_class = "", ""
            if delta is not None and abs(delta) > 1e-9:
                going_up = delta > 0
                is_good = going_up if higher_is_better else not going_up
                arrow = "▲" if going_up else "▼"
                color_class = "delta-good" if is_good else "delta-bad"
                value_class = f"dc-value-pulse {'value-good' if is_good else 'value-bad'}"
                badge_html = f'<span class="dc-delta-badge {color_class}">{arrow} {value_fmt(abs(delta))}</span>'
            else:
                value_class = "dc-value-pulse"

            with container:
                st.markdown(
                    f"""<div class="dc-impact-value"><span class="{value_class}">{value_fmt(value)}</span>{badge_html}</div>
                    <div class="dc-impact-label">{label}</div>""",
                    unsafe_allow_html=True,
                )

        impact_cols = st.columns(3)
        render_impact_metric(impact_cols[0], "prev_dari_reservoir", sim_dari_reservoir,
                              lambda v: f"{v:,.1f}", "m³/hari diambil dari waduk", higher_is_better=False)
        render_impact_metric(impact_cols[1], "prev_recycled_volume", sim_recycled_volume,
                              lambda v: f"{v:,.1f}", "m³/hari diselamatkan (recycle)", higher_is_better=True)
        render_impact_metric(impact_cols[2], "prev_nongsa_impact_pct", nongsa_impact_pct,
                              lambda v: f"{v:.3f}%", "dari volume Nongsa/hari", higher_is_better=False)

    # --- Aksi ---
    dc_risk_label = "belum berisiko" if nongsa_impact_pct < 0.05 else "perlu diwaspadai"
    st.markdown(
        f"""
        <div style="margin-top: 18px;">
            <div class="dc-sim-title" style="margin-bottom: 10px;">Aksi</div>
            <div class="action-chip-row">
                <span class="action-chip">Kota: tidak perlu tindakan darurat</span>
                <span class="action-chip">Nongsa: pasang alarm di {SIAGA_THRESHOLD + 5}%</span>
                <span class="action-chip">DC saat ini: {dc_risk_label}</span>
                <span class="action-chip">Naikkan recycle → hemat air, tambah listrik</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # --- Sumber & keterbatasan data ---
    def format_period(period_str):
        months_id = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
        try:
            start_str, end_str = period_str.split(" to ")
            start, end = pd.Timestamp(start_str), pd.Timestamp(end_str)
            return f"{months_id[start.month - 1]} {start.year}–{months_id[end.month - 1]} {end.year}"
        except (ValueError, IndexError):
            return period_str

    notes = dashboard_data.get("data_notes", {})
    st.markdown(
        f"""
        <div class="data-notes-card">
            <div class="data-notes-title">▾ Sumber &amp; keterbatasan data</div>
            <div class="data-notes-row"><b>Data riil:</b> {format_period(notes.get('real_period', '-'))},
                sumber {notes.get('real_source', '-')}.</div>
            <div class="data-notes-row"><b>Data simulasi:</b> {format_period(notes.get('simulated_period', '-'))},
                {notes.get('simulated_method', '-')} (bukan observasi aktual).</div>
            <div class="data-notes-row"><b>Koefisien DC:</b> WUE, energi recycle, dan recovery rate memakai
                asumsi benchmark industri, bukan hasil pengukuran DC Batam riil.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.markdown(
    '<div class="app-footer">TirtaFlow — Batam Singapore Hackathon 2026</div>',
    unsafe_allow_html=True,
)
