"""
Medical AI Cancer Detection Dashboard v4.0 — Production Ready
==============================================================
Uses MedicalAIEngine for unified inference.
"""

import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import pandas as pd
import random
import os
import sys
import logging
import datetime
from PIL import Image, ImageTk
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from medical_ai_engine import MedicalAIEngine, PredictionBatch
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS   # PyInstaller temp folder
    return os.path.dirname(os.path.abspath(__file__))
def resource_path(filename):
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.dirname(__file__), filename)
# ═══════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════
SCRIPT_DIR = get_base_dir()
LOG_PATH = os.path.join(SCRIPT_DIR, "medical_ai.log")
BASE_DIR = get_base_dir()
engine = MedicalAIEngine(base_dir=BASE_DIR)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MedicalAI")
log.info("=" * 60)
log.info("Dashboard v4.0 starting")

# ═══════════════════════════════════════════
# ENGINE INIT
# ═══════════════════════════════════════════
engine = MedicalAIEngine(base_dir=SCRIPT_DIR, threshold=0.5)
load_results = engine.load_all_models()
log.info("Model loading: %s", load_results)

current_model_name = "breast"
engine.load_model(current_model_name)

# ═══════════════════════════════════════════
# COLOR PALETTE
# ═══════════════════════════════════════════
BG_DARK       = "#080e1a"
BG_SIDEBAR    = "#0d1528"
BG_CARD       = "#111c32"
BG_CARD_ALT   = "#162040"
ACCENT_BLUE   = "#00b4d8"
ACCENT_CYAN   = "#00e5ff"
ACCENT_PURPLE = "#7b61ff"
ACCENT_GREEN  = "#00e676"
ACCENT_RED    = "#ff5252"
ACCENT_AMBER  = "#ffab40"
TEXT_PRIMARY   = "#e8eaf6"
TEXT_SECONDARY = "#8892b0"
TEXT_DIM       = "#4a5575"
BORDER_COLOR   = "#1e2d4a"
HOVER_GLOW     = "#1a3a5c"
FONT           = "Segoe UI"

def _blend(fg, bg=BG_DARK, a=0.15):
    r1, g1, b1 = int(fg[1:3],16), int(fg[3:5],16), int(fg[5:7],16)
    r2, g2, b2 = int(bg[1:3],16), int(bg[3:5],16), int(bg[5:7],16)
    return "#{:02x}{:02x}{:02x}".format(
        int(r1*a+r2*(1-a)), int(g1*a+g2*(1-a)), int(b1*a+b2*(1-a)))

# ═══════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════
stats = {"total": 0, "cancer": 0, "healthy": 0, "accuracy": 0.0}
all_predictions: list[dict] = []
recent_pred_data: list[dict] = []

# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════
def rounded_rect(canvas, x1, y1, x2, y2, r=16, **kw):
    pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2, x2-r,y2,
           x1+r,y2, x1,y2, x1,y2-r, x1,y1+r, x1,y1, x1+r,y1]
    return canvas.create_polygon(pts, smooth=True, **kw)

def _styled_btn(parent, text, cmd, accent=ACCENT_BLUE):
    b = tk.Button(parent, text=text, command=cmd,
                  font=(FONT,10,"bold"), bg=accent, fg="black",
                  activebackground=accent, activeforeground="black",
                  bd=0, padx=18, pady=6, cursor="hand2", relief="flat")
    b.bind("<Enter>", lambda e: b.configure(bg=ACCENT_CYAN))
    b.bind("<Leave>", lambda e: b.configure(bg=accent))
    return b

# ═══════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════
root = tk.Tk()
root.title("\U0001f9e0 Medical AI System v2")
root.geometry("1400x850")
root.configure(bg=BG_DARK)
root.minsize(1200, 700)
try: root.state("zoomed")
except: pass

# ═══════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════
sidebar = tk.Frame(root, bg=BG_SIDEBAR, width=72)
sidebar.pack(side="left", fill="y")
sidebar.pack_propagate(False)

logo_cv = tk.Canvas(sidebar, width=72, height=68, bg=BG_SIDEBAR, highlightthickness=0)
logo_cv.pack(pady=(18,12))
global_logo_img = None
try:
    logo_path = os.path.join(SCRIPT_DIR, "logo.png")
    if os.path.exists(logo_path):
        img = Image.open(logo_path)
        # Resize to fit within 60x60 maintaining aspect ratio
        img.thumbnail((60, 60), Image.Resampling.LANCZOS)
        global_logo_img = ImageTk.PhotoImage(img)
        logo_cv.create_image(36, 34, image=global_logo_img)
    else:
        logo_cv.create_text(36, 34, text="\U0001f9e0", font=(FONT, 22))
except Exception as e:
    log.warning(f"Could not load logo.png: {e}")
    logo_cv.create_text(36, 34, text="\U0001f9e0", font=(FONT, 22))

PAGE_KEYS = ["dashboard", "upload", "predictions", "analytics", "models", "settings"]
nav_meta = [
    ("\u2302","Dashboard"), ("\U0001f4c2","Upload CSV"), ("\U0001f50d","Predictions"),
    ("\U0001f4ca","Analytics"), ("\U0001f916","Models"), ("\u2699","Settings"),
]
nav_canvases: list[tk.Canvas] = []
active_page = "dashboard"

def navigate_to(page_key: str):
    global active_page
    if page_key not in pages: return
    active_page = page_key
    pages[page_key].tkraise()
    _highlight_sidebar()
    log.info("Navigation → %s", page_key)
    if page_key == "dashboard": _redraw_dashboard_chart()
    elif page_key == "analytics": _redraw_analytics_chart()

def _highlight_sidebar():
    idx = PAGE_KEYS.index(active_page) if active_page in PAGE_KEYS else 0
    for i, cv in enumerate(nav_canvases):
        cv.delete("all")
        icon = nav_meta[i][0]
        if i == idx:
            rounded_rect(cv, 4,4,48,44, r=12, fill="#0e2a4a", outline=ACCENT_BLUE, width=1.5)
            cv.create_text(26, 24, text=icon, font=(FONT,16), fill=ACCENT_CYAN)
        else:
            cv.create_text(26, 24, text=icon, font=(FONT,16), fill=TEXT_DIM)

def _make_nav(icon, tooltip, idx):
    cv = tk.Canvas(sidebar, width=52, height=48, bg=BG_SIDEBAR, highlightthickness=0, cursor="hand2")
    if idx == 0:
        rounded_rect(cv, 4,4,48,44, r=12, fill="#0e2a4a", outline=ACCENT_BLUE, width=1.5)
        cv.create_text(26, 24, text=icon, font=(FONT,16), fill=ACCENT_CYAN)
    else:
        cv.create_text(26, 24, text=icon, font=(FONT,16), fill=TEXT_DIM)
    page_key = PAGE_KEYS[idx]
    cv.bind("<Button-1>", lambda e, pk=page_key: navigate_to(pk))
    tip_win = [None]
    def _enter(e):
        tip_win[0] = tw = tk.Toplevel(root)
        tw.wm_overrideredirect(True)
        tw.geometry(f"+{e.x_root+20}+{e.y_root}")
        tk.Label(tw, text=tooltip, bg="#1a2744", fg=TEXT_PRIMARY, font=(FONT,9), padx=8, pady=3).pack()
    def _leave(e):
        if tip_win[0]: tip_win[0].destroy(); tip_win[0] = None
    cv.bind("<Enter>", _enter)
    cv.bind("<Leave>", _leave)
    cv.pack(pady=4)
    return cv

for i, (ic, tip) in enumerate(nav_meta):
    nav_canvases.append(_make_nav(ic, tip, i))

tk.Label(sidebar, text="v4.0", font=(FONT,7), fg=TEXT_DIM, bg=BG_SIDEBAR).pack(side="bottom", pady=10)

# ═══════════════════════════════════════════
# PAGE CONTAINER
# ═══════════════════════════════════════════
container = tk.Frame(root, bg=BG_DARK)
container.pack(side="left", fill="both", expand=True)
pages: dict[str, tk.Frame] = {}

def _make_page(key) -> tk.Frame:
    f = tk.Frame(container, bg=BG_DARK)
    f.grid(row=0, column=0, sticky="nsew")
    pages[key] = f
    return f

container.rowconfigure(0, weight=1)
container.columnconfigure(0, weight=1)

# ═══════════════════════════════════════════
# PAGE 1: DASHBOARD
# ═══════════════════════════════════════════
pg_dash = _make_page("dashboard")

_hdr = tk.Frame(pg_dash, bg=BG_DARK)
_hdr.pack(fill="x", padx=28, pady=(22,2))
tk.Label(_hdr, text="\U0001f9e0 AI Cancer Detection System",
         font=(FONT,24,"bold"), fg=TEXT_PRIMARY, bg=BG_DARK).pack(side="left")
_hdr_r = tk.Frame(_hdr, bg=BG_DARK)
_hdr_r.pack(side="right")
for _ic in ["\U0001f514", "\u2699"]:
    tk.Label(_hdr_r, text=_ic, font=(FONT,16), fg=TEXT_SECONDARY, bg=BG_DARK, padx=8).pack(side="left")

tk.Label(pg_dash, text="Production-ready unified AI inference • Upload patient data for instant predictions.",
         font=(FONT,11), fg=TEXT_SECONDARY, bg=BG_DARK).pack(anchor="w", padx=30, pady=(0,10))

# -- model selector (UI removed as requested) --
model_var = tk.StringVar(value="breast")

def _on_model_change(*_):
    global current_model_name
    current_model_name = model_var.get()
    engine.load_model(current_model_name)
    _update_footer()
    _redraw_dashboard_chart()
    log.info("Model switched → %s", current_model_name)

# -- threshold slider --
_thr = tk.Frame(pg_dash, bg=BG_DARK)
_thr.pack(fill="x", padx=28, pady=(0,6))
tk.Label(_thr, text="Confidence Threshold:", font=(FONT,9), fg=TEXT_SECONDARY, bg=BG_DARK).pack(side="left")
threshold_var = tk.DoubleVar(value=0.5)
threshold_label = tk.Label(_thr, text="50%", font=(FONT,9,"bold"), fg=ACCENT_CYAN, bg=BG_DARK)
threshold_label.pack(side="right", padx=8)

def _on_threshold_change(val):
    v = float(val)
    engine.set_threshold(v)
    threshold_label.config(text=f"{v*100:.0f}%")

threshold_slider = tk.Scale(_thr, from_=0.1, to=0.9, resolution=0.05,
    orient="horizontal", variable=threshold_var, command=_on_threshold_change,
    bg=BG_DARK, fg=TEXT_PRIMARY, troughcolor=BG_CARD, highlightthickness=0,
    sliderrelief="flat", length=200, showvalue=False)
threshold_slider.pack(side="right")

# -- stat cards --
cards_frame = tk.Frame(pg_dash, bg=BG_DARK)
cards_frame.pack(fill="x", padx=24, pady=(4,10))

card_info = [
    ("total","Total Patients","\U0001f465",ACCENT_BLUE),
    ("cancer","Cancer Detected","\u26a0\ufe0f",ACCENT_RED),
    ("healthy","Healthy Cases","\u2714\ufe0f",ACCENT_GREEN),
    ("accuracy","Avg Confidence","\U0001f4c8",ACCENT_PURPLE),
]
card_val_ids: dict = {}

for _ci, (k, label, icon, color) in enumerate(card_info):
    CW, CH = 240, 120
    _cv = tk.Canvas(cards_frame, width=CW, height=CH, bg=BG_DARK, highlightthickness=0)
    _cv.grid(row=0, column=_ci, padx=8, pady=4, sticky="nsew")
    rounded_rect(_cv, 0,0,CW,CH, r=16, fill=BG_CARD, outline=BORDER_COLOR)
    _cx, _cy, _cr = 36, 38, 18
    _cv.create_oval(_cx-_cr,_cy-_cr,_cx+_cr,_cy+_cr, fill=_blend(color,BG_CARD), outline="")
    _cv.create_text(_cx, _cy, text=icon, font=(FONT,14))
    _vid = _cv.create_text(90, 34, anchor="w",
        text="0" if k != "accuracy" else "0.0%", font=(FONT,22,"bold"), fill=TEXT_PRIMARY)
    _cv.create_text(90, 62, anchor="w", text=label, font=(FONT,9), fill=TEXT_SECONDARY)
    _pts = [random.randint(85,105) for _ in range(8)]
    for _j in range(7):
        _cv.create_line(90+_j*18,_pts[_j], 90+(_j+1)*18,_pts[_j+1], fill=color, width=1.5, smooth=True)
    card_val_ids[k] = (_cv, _vid)
    cards_frame.columnconfigure(_ci, weight=1)

def _refresh_cards():
    for k, (cv, vid) in card_val_ids.items():
        if k == "accuracy": cv.itemconfig(vid, text=f"{stats[k]:.1f}%")
        else: cv.itemconfig(vid, text=str(stats[k]))

# -- dashboard lower split --
dash_lower = tk.Frame(pg_dash, bg=BG_DARK)
dash_lower.pack(fill="both", expand=True, padx=24, pady=(0,6))

dash_chart_frame = tk.Frame(dash_lower, bg=BG_DARK)
dash_chart_frame.pack(side="left", fill="both", expand=True, padx=(0,8))
tk.Label(dash_chart_frame, text="\U0001f4ca  Confidence Overview",
         font=(FONT,13,"bold"), fg=TEXT_PRIMARY, bg=BG_DARK).pack(anchor="w")

dash_chart_cv = tk.Canvas(dash_chart_frame, bg=BG_CARD, highlightthickness=0, height=220)
dash_chart_cv.pack(fill="both", expand=True, pady=4)

def _redraw_dashboard_chart():
    cv = dash_chart_cv; cv.delete("all")
    w = cv.winfo_width() or 600; h = cv.winfo_height() or 220
    rounded_rect(cv, 0,0,w,h, r=14, fill=BG_CARD, outline=BORDER_COLOR)
    title = f"Model: {current_model_name.capitalize()}"
    task = engine.config["task"] if engine.config else "unknown"
    cv.create_text(w//2, 18, text=f"{title} ({task})", font=(FONT,10,"bold"), fill=TEXT_SECONDARY)
    if not all_predictions:
        cv.create_text(w//2, h//2, text="Upload a CSV to see results here.", font=(FONT,11), fill=TEXT_DIM)
        return
    data = all_predictions[-30:]
    n = len(data); bar_w = max(4, min(22, (w-80)//n)); gap = 2; base_y = h-30
    for i, d in enumerate(data):
        pct = d["confidence"]/100; bar_h = pct*(h-70)
        x = 40 + i*(bar_w+gap)
        color = ACCENT_RED if d["tag"] == "cancer" else ACCENT_GREEN
        cv.create_rectangle(x, base_y-bar_h, x+bar_w, base_y, fill=color, outline="")
    cv.create_line(36, base_y, 40+n*(bar_w+gap), base_y, fill=TEXT_DIM)
    lx = w-180
    cv.create_rectangle(lx,10,lx+10,20, fill=ACCENT_GREEN, outline="")
    cv.create_text(lx+14,15, text="Healthy", anchor="w", font=(FONT,8), fill=TEXT_SECONDARY)
    cv.create_rectangle(lx+80,10,lx+90,20, fill=ACCENT_RED, outline="")
    cv.create_text(lx+94,15, text="Cancer", anchor="w", font=(FONT,8), fill=TEXT_SECONDARY)

dash_chart_cv.bind("<Configure>", lambda e: _redraw_dashboard_chart())

# -- recent predictions panel --
dash_recent_frame = tk.Frame(dash_lower, bg=BG_CARD, width=270)
dash_recent_frame.pack(side="right", fill="y", padx=(8,0))
dash_recent_frame.pack_propagate(False)
tk.Label(dash_recent_frame, text="\U0001f552  Recent Predictions",
         font=(FONT,12,"bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(anchor="w", padx=14, pady=(14,8))
dash_recent_inner = tk.Frame(dash_recent_frame, bg=BG_CARD)
dash_recent_inner.pack(fill="both", expand=True, padx=8, pady=(0,10))

def _rebuild_recent_panel():
    for w in dash_recent_inner.winfo_children(): w.destroy()
    if not recent_pred_data:
        tk.Label(dash_recent_inner, text="No predictions yet.",
                 font=(FONT,9), fg=TEXT_DIM, bg=BG_CARD).pack(pady=20)
        return
    for d in recent_pred_data:
        _add_recent_entry(dash_recent_inner, d)

def _add_recent_entry(parent, d):
    f = tk.Frame(parent, bg=BG_CARD_ALT, pady=2)
    f.pack(fill="x", pady=3, padx=4)
    tag_color = ACCENT_RED if d["tag"] == "cancer" else ACCENT_GREEN
    dot = tk.Canvas(f, width=10, height=10, bg=BG_CARD_ALT, highlightthickness=0)
    dot.pack(side="left", padx=(6,4), pady=8)
    dot.create_oval(1,1,9,9, fill=tag_color, outline="")
    info = tk.Frame(f, bg=BG_CARD_ALT)
    info.pack(side="left", fill="x", expand=True, padx=4, pady=4)
    tk.Label(info, text=f"Patient #{d['idx']}", font=(FONT,9,"bold"),
             fg=TEXT_PRIMARY, bg=BG_CARD_ALT, anchor="w").pack(anchor="w")
    tk.Label(info, text=f"{d['model_used'].capitalize()} • {d['confidence']:.1f}% • {d['status_short']}",
             font=(FONT,8), fg=TEXT_SECONDARY, bg=BG_CARD_ALT, anchor="w").pack(anchor="w")

_rebuild_recent_panel()

# -- footer --
footer_frame = tk.Frame(pg_dash, bg=BG_DARK)
footer_frame.pack(fill="x", padx=28, pady=(0,8))
footer_label = tk.Label(footer_frame,
    text=f"\u26a1 Active: {current_model_name.capitalize()} | Engine v2.0",
    font=(FONT,9), fg=TEXT_DIM, bg=BG_DARK)
footer_label.pack(side="left")
tk.Label(footer_frame, text="Medical AI Dashboard v4.0",
         font=(FONT,9), fg=TEXT_DIM, bg=BG_DARK).pack(side="right")

def _update_footer():
    info = engine.get_model_info()
    task = info.get("task", "unknown")
    footer_label.config(text=f"\u26a1 Active: {current_model_name.capitalize()} | {task} | Engine v2.0")

# ═══════════════════════════════════════════
# PAGE 2: UPLOAD CSV
# ═══════════════════════════════════════════
pg_upload = _make_page("upload")
_u_hdr = tk.Frame(pg_upload, bg=BG_DARK)
_u_hdr.pack(fill="x", padx=28, pady=(22,10))
tk.Label(_u_hdr, text="\U0001f4c2 Upload CSV", font=(FONT,22,"bold"),
         fg=TEXT_PRIMARY, bg=BG_DARK).pack(side="left")
_styled_btn(_u_hdr, "\u2302 Dashboard", lambda: navigate_to("dashboard")).pack(side="right")

tk.Label(pg_upload, text="Select a CSV file — features are auto-aligned, no format errors possible.",
         font=(FONT,11), fg=TEXT_SECONDARY, bg=BG_DARK).pack(anchor="w", padx=30, pady=(0,20))

upload_status_lbl = tk.Label(pg_upload, text="No file loaded.",
                              font=(FONT,12), fg=TEXT_DIM, bg=BG_DARK)
upload_status_lbl.pack(pady=10)

upload_result_text = tk.Text(pg_upload, bg=BG_CARD, fg=TEXT_PRIMARY,
                              font=(FONT,10), height=18, wrap="word",
                              insertbackground=TEXT_PRIMARY, bd=0,
                              highlightthickness=1, highlightcolor=BORDER_COLOR)
upload_result_text.pack(fill="both", expand=True, padx=30, pady=(0,10))
upload_result_text.config(state="disabled")

# Tag configurations for colored output
upload_result_text.tag_configure("header", foreground=ACCENT_CYAN, font=(FONT,11,"bold"))
upload_result_text.tag_configure("cancer", foreground=ACCENT_RED)
upload_result_text.tag_configure("healthy", foreground=ACCENT_GREEN)
upload_result_text.tag_configure("summary", foreground=ACCENT_AMBER, font=(FONT,10,"bold"))

def predict_csv():
    """Core prediction using MedicalAIEngine."""
    global stats, all_predictions, recent_pred_data, current_model_name

    file_path = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv")])
    if not file_path: return
    log.info("CSV selected: %s", file_path)

    try:
        df = pd.read_csv(file_path)
    except Exception as e:
        log.error("CSV read error: %s", e)
        messagebox.showerror("Error", f"Could not read CSV.\n\n{e}")
        return

    if df.empty:
        messagebox.showwarning("Empty CSV", "The selected CSV file has no data rows.")
        return

    # Try to auto-detect model from CSV features
    detected_model = engine.detect_cancer_type(df)
    if detected_model:
        current_model_name = detected_model
        model_var.set(detected_model)
        _update_footer()
        log.info("Auto-detected model: %s", detected_model)

    # Ensure correct model is loaded
    if not engine.load_model(current_model_name):
        error_msg = getattr(engine, 'last_error', 'Unknown Error')
        messagebox.showerror("Model Error", f"Failed to load model '{current_model_name}'.\n\nReason: {error_msg}\n\nPlease check logs for more details.")
        return

    # Run prediction through engine
    try:
        batch = engine.predict(df)
    except Exception as e:
        log.error("Engine prediction failed: %s", e)
        messagebox.showerror("Prediction Error", f"Failed to run prediction:\n\n{e}")
        return

    # Convert to UI format
    all_predictions.clear()
    recent_pred_data.clear()

    upload_result_text.config(state="normal")
    upload_result_text.delete("1.0", "end")

    upload_result_text.insert("end",
        f"{'═'*60}\n  PREDICTION REPORT — {current_model_name.capitalize()} Model\n"
        f"  Task: {engine.config['task']}  |  Threshold: {engine.get_threshold()*100:.0f}%\n"
        f"  Patients: {batch.total}\n{'═'*60}\n\n", "header")

    for i, p in enumerate(batch.predictions):
        tag = "cancer" if p.cancer_detected else "no_cancer"
        row_dict = {
            "idx": p.patient_id, "status": p.status,
            "confidence": p.confidence, "tag": tag,
            "model_used": current_model_name,
            "status_short": "YES" if p.cancer_detected else "NO",
            "cancer_detected": p.cancer_detected,
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "raw_data": df.iloc[i].to_dict() if i < len(df) else {}
        }
        all_predictions.append(row_dict)

        # Colored output
        if p.cancer_detected:
            line = (
                f"  Patient #{p.patient_id}:\n"
                f"    Cancer detected\n"
                f"    Type: {p.cancer_type.upper() if p.cancer_type else 'UNKNOWN'}\n"
                f"    Nature: {p.status}\n"
                f"    Confidence: {p.confidence:.1f}%\n\n"
            )
            text_tag = "cancer"
        else:
            line = (
                f"  Patient #{p.patient_id}:\n"
                f"    No cancer detected (Healthy)\n"
                f"    Confidence: {p.confidence:.1f}%\n\n"
            )
            text_tag = "healthy"
        upload_result_text.insert("end", line, text_tag)

    # Summary
    upload_result_text.insert("end",
        f"\n{'─'*60}\n"
        f"  SUMMARY: {batch.cancer_count} cancer / {batch.healthy_count} healthy / "
        f"{batch.avg_confidence:.1f}% avg confidence\n", "summary")

    if batch.errors:
        upload_result_text.insert("end", f"\n  ⚠ Warnings: {len(batch.errors)}\n")
        for err in batch.errors[:5]:
            upload_result_text.insert("end", f"    • {err}\n")

    recent_pred_data.extend(all_predictions[-20:])

    stats["total"] = batch.total
    stats["cancer"] = batch.cancer_count
    stats["healthy"] = batch.healthy_count
    stats["accuracy"] = batch.avg_confidence

    _refresh_cards()
    _rebuild_recent_panel()
    _rebuild_predictions_list()
    _redraw_dashboard_chart()
    _redraw_analytics_chart()

    upload_status_lbl.config(
        text=f"\u2714  {batch.total} patients from {os.path.basename(file_path)}  "
             f"({batch.cancer_count} cancer, {batch.healthy_count} healthy)",
        fg=ACCENT_GREEN)

    log.info("Predictions: %d patients, %d cancer, %d healthy",
             batch.total, batch.cancer_count, batch.healthy_count)

    upload_result_text.config(state="disabled")

upload_btn_frame = tk.Frame(pg_upload, bg=BG_DARK)
upload_btn_frame.pack(pady=10)
_styled_btn(upload_btn_frame, "\U0001f4c2  Upload CSV File", predict_csv, accent=ACCENT_BLUE).pack()

# ═══════════════════════════════════════════
# PAGE 3: PREDICTIONS
# ═══════════════════════════════════════════
pg_pred = _make_page("predictions")
_p_hdr = tk.Frame(pg_pred, bg=BG_DARK)
_p_hdr.pack(fill="x", padx=28, pady=(22,10))
tk.Label(_p_hdr, text="\U0001f50d Prediction Results", font=(FONT,22,"bold"),
         fg=TEXT_PRIMARY, bg=BG_DARK).pack(side="left")
_styled_btn(_p_hdr, "\u2302 Dashboard", lambda: navigate_to("dashboard")).pack(side="right")
_styled_btn(_p_hdr, "\U0001f4c2 Upload CSV", predict_csv, accent=ACCENT_GREEN).pack(side="right", padx=8)

pred_canvas = tk.Canvas(pg_pred, bg=BG_DARK, highlightthickness=0)
pred_sb = ttk.Scrollbar(pg_pred, orient="vertical", command=pred_canvas.yview)
pred_inner = tk.Frame(pred_canvas, bg=BG_DARK)
pred_inner.bind("<Configure>", lambda e: pred_canvas.configure(scrollregion=pred_canvas.bbox("all")))
pred_canvas.create_window((0,0), window=pred_inner, anchor="nw")
pred_canvas.configure(yscrollcommand=pred_sb.set)
pred_canvas.pack(side="left", fill="both", expand=True, padx=(28,0), pady=(0,10))
pred_sb.pack(side="right", fill="y", padx=(0,10))

def _on_pred_mousewheel(event):
    pred_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
pred_canvas.bind("<Enter>", lambda e: pred_canvas.bind_all("<MouseWheel>", _on_pred_mousewheel))
pred_canvas.bind("<Leave>", lambda e: pred_canvas.unbind_all("<MouseWheel>"))

# column headers
def _generate_single_pdf(d):
    default_name = f"Patient_Report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    save_path = filedialog.asksaveasfilename(
        defaultextension=".pdf",
        initialfile=default_name,
        title="Save PDF Report",
        filetypes=[("PDF Files", "*.pdf")]
    )
    if not save_path:
        return

    try:
        doc = SimpleDocTemplate(save_path, pagesize=letter)
        Story = []
        styles = getSampleStyleSheet()
        
        # 1. Logo
        logo_path = os.path.join(SCRIPT_DIR, "logo.png")
        if os.path.exists(logo_path):
            try:
                # Add logo. We resize it to 80x80 approximately or proportionally
                rl_img = RLImage(logo_path, width=80, height=80)
                rl_img.hAlign = 'CENTER'
                Story.append(rl_img)
                Story.append(Spacer(1, 12))
            except Exception as e:
                log.warning(f"Could not load logo into PDF: {e}")

        # 2. Title & Date
        title_style = ParagraphStyle(
            'ReportTitle',
            parent=styles['Heading1'],
            alignment=1, # Center
            spaceAfter=12
        )
        Story.append(Paragraph("AI Cancer Detection System Report", title_style))
        
        Story.append(Paragraph(f"<b>Generated on:</b> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Normal"]))
        Story.append(Spacer(1, 12))

        # 3. Patient Info & Prediction
        Story.append(Paragraph("<b>Prediction Results</b>", styles["Heading2"]))
        Story.append(Paragraph(f"<b>Patient ID:</b> {d['idx']}", styles["Normal"]))
        Story.append(Paragraph(f"<b>Model Used:</b> {d['model_used'].capitalize()}", styles["Normal"]))
        
        is_cancer = d.get("cancer_detected", d["tag"]=="cancer")
        status_text = d["status"] if d["status"] else ("Cancer Detected" if is_cancer else "No Cancer Detected")
        Story.append(Paragraph(f"<b>Prediction Status:</b> {status_text}", styles["Normal"]))
        Story.append(Paragraph(f"<b>Confidence:</b> {d['confidence']:.1f}%", styles["Normal"]))
        
        Story.append(Spacer(1, 12))
        
        # Interpretation
        Story.append(Paragraph("<b>Interpretation</b>", styles["Heading3"]))
        if is_cancer:
            interp = "The model suggests that further medical evaluation is recommended."
        else:
            interp = "The model does not indicate signs of cancer based on the provided data."
        Story.append(Paragraph(interp, styles["Normal"]))
        Story.append(Spacer(1, 20))

        # 4. Patient Input Values Table
        Story.append(Paragraph("<b>Patient Input Data</b>", styles["Heading2"]))
        raw_data = d.get("raw_data", {})
        if raw_data:
            table_data = [["Feature", "Value"]]
            for k, v in raw_data.items():
                table_data.append([str(k), str(v)])
            
            t = Table(table_data, colWidths=[200, 200])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor(ACCENT_BLUE)),
                ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
                ('ALIGN', (0,0), (-1,-1), 'LEFT'),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0,0), (-1,0), 12),
                ('BACKGROUND', (0,1), (-1,-1), colors.beige),
                ('GRID', (0,0), (-1,-1), 1, colors.black),
            ]))
            Story.append(t)
        else:
            Story.append(Paragraph("No input data available.", styles["Normal"]))
            
        Story.append(Spacer(1, 40))
        
        # 5. Footer
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Italic'],
            fontSize=8,
            textColor=colors.gray,
            alignment=1
        )
        Story.append(Paragraph("This report is generated by an AI-assisted prediction system and is not a substitute for professional medical diagnosis.", footer_style))

        doc.build(Story)
        messagebox.showinfo("Success", "PDF report generated successfully.")
    except Exception as e:
        log.error(f"PDF generation failed: {e}")
        messagebox.showerror("Error", f"Failed to generate PDF report.\n\n{e}")

_col_hdr = tk.Frame(pred_inner, bg=BG_CARD, pady=2)
_col_hdr.pack(fill="x", pady=(0,2))
for t, w in [("Patient",10),("Cancer?",8),("Status",20),("Confidence",10),("Model",10), ("Action",12)]:
    tk.Label(_col_hdr, text=t, font=(FONT,9,"bold"), fg=TEXT_DIM,
             bg=BG_CARD, width=w, anchor="center").pack(side="left", padx=6, pady=4)

def _rebuild_predictions_list():
    children = pred_inner.winfo_children()
    for child in children[1:]: child.destroy()
    if not all_predictions:
        tk.Label(pred_inner, text="No predictions yet. Upload a CSV first.",
                 font=(FONT,11), fg=TEXT_DIM, bg=BG_DARK).pack(pady=40)
        return
    for d in all_predictions: _add_pred_row(d)
    pred_canvas.yview_moveto(0)

def _add_pred_row(d):
    bg = BG_CARD if d["idx"]%2==0 else BG_CARD_ALT
    rf = tk.Frame(pred_inner, bg=bg, pady=1)
    rf.pack(fill="x", pady=1, padx=2)
    is_cancer = d.get("cancer_detected", d["tag"]=="cancer")
    av_color = ACCENT_RED if is_cancer else ACCENT_GREEN
    av = tk.Canvas(rf, width=36, height=36, bg=bg, highlightthickness=0)
    av.pack(side="left", padx=(10,4), pady=6)
    av.create_oval(2,2,34,34, fill=_blend(av_color,bg,0.2), outline=av_color, width=1.5)
    av.create_text(18, 18, text=f"P{d['idx']}", font=(FONT,8,"bold"), fill=av_color)
    tk.Label(rf, text=f"#{d['idx']}", font=(FONT,11,"bold"),
             fg=TEXT_PRIMARY, bg=bg, width=6, anchor="w").pack(side="left", padx=6)
    # Cancer YES/NO badge
    yn_bg = ACCENT_RED if is_cancer else ACCENT_GREEN
    yn_text = "YES" if is_cancer else "NO"
    tk.Label(rf, text=f" {yn_text} ", font=(FONT,9,"bold"),
             fg="white", bg=yn_bg, padx=6, pady=2).pack(side="left", padx=6)
    # Status
    tk.Label(rf, text=d["status"], font=(FONT,9),
             fg=TEXT_PRIMARY, bg=bg, width=18, anchor="w").pack(side="left", padx=6)
    # Confidence
    cc = ACCENT_RED if d["confidence"]<70 else (ACCENT_AMBER if d["confidence"]<90 else ACCENT_GREEN)
    tk.Label(rf, text=f"{d['confidence']:.1f}%", font=(FONT,12,"bold"),
             fg=cc, bg=bg, width=8).pack(side="left", padx=6)
    # Model
    tk.Label(rf, text=d["model_used"].capitalize(), font=(FONT,9),
             fg=TEXT_SECONDARY, bg=bg, width=10).pack(side="left", padx=6)
             
    # PDF Button
    _styled_btn(rf, "Print PDF Report", lambda: _generate_single_pdf(d), accent=ACCENT_BLUE).pack(side="right", padx=10)

    def _enter(e, f=rf, b=bg):
        f.configure(bg=HOVER_GLOW)
        for c in f.winfo_children():
            try: c.configure(bg=HOVER_GLOW)
            except: pass
    def _leave(e, f=rf, b=bg):
        f.configure(bg=b)
        for c in f.winfo_children():
            try: c.configure(bg=b)
            except: pass
    rf.bind("<Enter>", _enter); rf.bind("<Leave>", _leave)

_rebuild_predictions_list()

# ═══════════════════════════════════════════
# PAGE 4: ANALYTICS
# ═══════════════════════════════════════════
pg_analytics = _make_page("analytics")
_a_hdr = tk.Frame(pg_analytics, bg=BG_DARK)
_a_hdr.pack(fill="x", padx=28, pady=(22,10))
tk.Label(_a_hdr, text="\U0001f4ca Analytics", font=(FONT,22,"bold"),
         fg=TEXT_PRIMARY, bg=BG_DARK).pack(side="left")
_styled_btn(_a_hdr, "\u2302 Dashboard", lambda: navigate_to("dashboard")).pack(side="right")

analytics_chart = tk.Canvas(pg_analytics, bg=BG_CARD, highlightthickness=0, height=350)
analytics_chart.pack(fill="both", expand=True, padx=30, pady=(0,10))
analytics_summary = tk.Label(pg_analytics, text="", font=(FONT,11),
                              fg=TEXT_SECONDARY, bg=BG_DARK, justify="left")
analytics_summary.pack(anchor="w", padx=30, pady=(0,16))

def _redraw_analytics_chart():
    cv = analytics_chart; cv.delete("all")
    w = cv.winfo_width() or 800; h = cv.winfo_height() or 350
    rounded_rect(cv, 0,0,w,h, r=14, fill=BG_CARD, outline=BORDER_COLOR)
    cv.create_text(w//2, 24, text=f"Prediction Distribution — {current_model_name.capitalize()} Model",
                   font=(FONT,13,"bold"), fill=TEXT_PRIMARY)
    if not all_predictions:
        cv.create_text(w//2, h//2, text="No data yet. Upload a CSV from the Upload page.",
                       font=(FONT,12), fill=TEXT_DIM)
        analytics_summary.config(text=""); return
    cancer_n = stats["cancer"]; healthy_n = stats["healthy"]; total = stats["total"] or 1
    cx, cy, r = 160, h//2+10, min(100, h//3)
    cancer_angle = (cancer_n/total)*360; healthy_angle = 360-cancer_angle
    cv.create_arc(cx-r,cy-r,cx+r,cy+r, start=0, extent=healthy_angle, fill=ACCENT_GREEN, outline=BG_CARD, width=2)
    cv.create_arc(cx-r,cy-r,cx+r,cy+r, start=healthy_angle, extent=cancer_angle, fill=ACCENT_RED, outline=BG_CARD, width=2)
    cv.create_text(cx, cy-r-18, text="Cancer vs Healthy", font=(FONT,9,"bold"), fill=TEXT_SECONDARY)
    cv.create_rectangle(cx-r,cy+r+14,cx-r+12,cy+r+26, fill=ACCENT_GREEN, outline="")
    cv.create_text(cx-r+16,cy+r+20, text=f"Healthy: {healthy_n}", anchor="w", font=(FONT,8), fill=TEXT_SECONDARY)
    cv.create_rectangle(cx+20,cy+r+14,cx+32,cy+r+26, fill=ACCENT_RED, outline="")
    cv.create_text(cx+36,cy+r+20, text=f"Cancer: {cancer_n}", anchor="w", font=(FONT,8), fill=TEXT_SECONDARY)
    bx = 360; by_top = 60; by_bot = h-40; bar_area_w = w-bx-40
    n = len(all_predictions); bar_w = max(3, min(18, bar_area_w//max(n,1))); gap = 1
    cv.create_text(bx+bar_area_w//2, by_top-14, text="Per-Patient Confidence", font=(FONT,9,"bold"), fill=TEXT_SECONDARY)
    for i, d in enumerate(all_predictions):
        pct = d["confidence"]/100; bh = pct*(by_bot-by_top-10)
        x1 = bx+i*(bar_w+gap); color = ACCENT_RED if d["tag"]=="cancer" else ACCENT_GREEN
        cv.create_rectangle(x1, by_bot-bh, x1+bar_w, by_bot, fill=color, outline="")
    cv.create_line(bx, by_bot, bx+n*(bar_w+gap), by_bot, fill=TEXT_DIM)
    analytics_summary.config(
        text=f"Total: {total}  |  Cancer: {cancer_n}  |  Healthy: {healthy_n}  |  "
             f"Avg Confidence: {stats['accuracy']:.1f}%  |  Model: {current_model_name.capitalize()}")

analytics_chart.bind("<Configure>", lambda e: _redraw_analytics_chart())

# ═══════════════════════════════════════════
# PAGE 5: MODELS
# ═══════════════════════════════════════════
pg_models = _make_page("models")
_m_hdr = tk.Frame(pg_models, bg=BG_DARK)
_m_hdr.pack(fill="x", padx=28, pady=(22,10))
tk.Label(_m_hdr, text="\U0001f916 Models", font=(FONT,22,"bold"),
         fg=TEXT_PRIMARY, bg=BG_DARK).pack(side="left")
_styled_btn(_m_hdr, "\u2302 Dashboard", lambda: navigate_to("dashboard")).pack(side="right")
tk.Label(pg_models, text="Select and inspect available ML models.",
         font=(FONT,11), fg=TEXT_SECONDARY, bg=BG_DARK).pack(anchor="w", padx=30, pady=(0,20))
models_detail_label = tk.Label(pg_models, text="", font=(FONT,11),
                                fg=TEXT_PRIMARY, bg=BG_DARK, justify="left")
models_detail_label.pack(anchor="w", padx=30, pady=10)

def _select_model_page(key):
    model_var.set(key); _on_model_change(); _update_models_info()
    log.info("Model selected on Models page: %s", key)

def _update_models_info():
    info = engine.get_model_info()
    lines = [
        f"Current Model: {info.get('disease','N/A').capitalize()}",
        f"Model Type: {info.get('model_type','N/A')}",
        f"Task: {info.get('task','N/A')}",
        f"Features Expected: {info.get('n_features',0)}",
        f"Encoders: {info.get('n_encoders',0)}",
        f"Threshold: {info.get('threshold',0.5)*100:.0f}%",
        f"", f"Total predictions this session: {stats['total']}",
    ]
    models_detail_label.config(text="\n".join(lines))

model_btn_frame = tk.Frame(pg_models, bg=BG_DARK)
model_btn_frame.pack(pady=10)
for _mk in engine.get_available_models():
    _styled_btn(model_btn_frame, f"\U0001f9ec  {_mk.capitalize()}",
                lambda k=_mk: _select_model_page(k), accent=ACCENT_PURPLE).pack(side="left", padx=8)
_update_models_info()

# ═══════════════════════════════════════════
# PAGE 6: SETTINGS
# ═══════════════════════════════════════════
pg_settings = _make_page("settings")
_s_hdr = tk.Frame(pg_settings, bg=BG_DARK)
_s_hdr.pack(fill="x", padx=28, pady=(22,10))
tk.Label(_s_hdr, text="\u2699 Settings", font=(FONT,22,"bold"),
         fg=TEXT_PRIMARY, bg=BG_DARK).pack(side="left")
_styled_btn(_s_hdr, "\u2302 Dashboard", lambda: navigate_to("dashboard")).pack(side="right")
tk.Label(pg_settings, text="Application settings and log viewer.",
         font=(FONT,11), fg=TEXT_SECONDARY, bg=BG_DARK).pack(anchor="w", padx=30, pady=(0,20))

settings_info = tk.Frame(pg_settings, bg=BG_DARK)
settings_info.pack(fill="x", padx=30, pady=10)
tk.Label(settings_info, text=f"Log file:  {LOG_PATH}", font=(FONT,10), fg=TEXT_SECONDARY, bg=BG_DARK).pack(anchor="w")
tk.Label(settings_info, text=f"Script dir:  {SCRIPT_DIR}", font=(FONT,10), fg=TEXT_SECONDARY, bg=BG_DARK).pack(anchor="w")
tk.Label(settings_info, text=f"Engine:  MedicalAIEngine v2.0", font=(FONT,10), fg=ACCENT_CYAN, bg=BG_DARK).pack(anchor="w")

settings_log_text = tk.Text(pg_settings, bg=BG_CARD, fg=TEXT_PRIMARY,
                             font=(FONT,9), height=20, wrap="word",
                             insertbackground=TEXT_PRIMARY, bd=0,
                             highlightthickness=1, highlightcolor=BORDER_COLOR)
settings_log_text.pack(fill="both", expand=True, padx=30, pady=10)
settings_log_text.config(state="disabled")

def _refresh_log_view():
    settings_log_text.config(state="normal")
    settings_log_text.delete("1.0", "end")
    try:
        with open(LOG_PATH, "r") as f: settings_log_text.insert("end", f.read())
    except FileNotFoundError: settings_log_text.insert("end", "(log file not found)")
    settings_log_text.see("end")
    settings_log_text.config(state="disabled")

_styled_btn(pg_settings, "\u21bb  Refresh Log", _refresh_log_view).pack(pady=6)

# ═══════════════════════════════════════════
# TTK STYLING + LAUNCH
# ═══════════════════════════════════════════
style = ttk.Style()
style.theme_use("clam")
style.configure("Vertical.TScrollbar", background=BG_CARD, troughcolor=BG_DARK,
                bordercolor=BG_DARK, arrowcolor=TEXT_DIM)

navigate_to("dashboard")
log.info("Application ready — Medical AI Dashboard v4.0")
root.mainloop()
