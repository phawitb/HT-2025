from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse, HTMLResponse
from linebot import LineBotApi, WebhookParser
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    FlexSendMessage,
)
import logging
import requests
import json
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta

TH_TZ = timezone(timedelta(hours=7))

def format_ts_th(s: str) -> str:
    """
    รับ string timestamp จาก GAS เช่น 2025-11-17T22:24:02.000Z
    คืน string แบบ 11/18/25-05:24 เวลาประเทศไทย
    """
    dt = _parse_dt(s)
    if dt == datetime.min:
        return s  # ถ้า parse ไม่ได้ก็ส่งคืนเหมือนเดิม

    # ถ้าไม่มี timezone ให้ถือว่าเป็น UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_th = dt.astimezone(TH_TZ)
    return dt_th.strftime("%m/%d/%y-%H:%M")


# =========================================================
# FastAPI app
# =========================================================
app = FastAPI()

# =========================================================
# 🔑 LINE credentials (Hardcoded)
# =========================================================
LINE_CHANNEL_SECRET = "23969ac940dc1ae6b5b5211b7c84807a"
LINE_CHANNEL_ACCESS_TOKEN = "irnHkqFbWyJW5SAVKPbqv9bITkPaZIXWNKlXfg7RKUYwLVNufpWJg7VtdzGEdMFYH25xngW9Nwx2Py/Kp1SVnH3iBkCiZUYgQDJUEBvarWzb/u3CbV1eB7/RGPbi+D9cwRt3pQECw5genf6N4UOn6wdB04t89/1O/w1cDnyilFU="

# 🌐 BASE URL ของเว็บเรา (ใช้สร้างลิงก์ให้ user คลิกจาก LINE)
# WEB_BASE_URL = "https://9c48c1744596.ngrok-free.app"  # <--- แก้ตรงนี้เวลาเปลี่ยน ngrok
WEB_BASE_URL = "https://ht-2025.onrender.com"

print(f"SECRET length: {len(LINE_CHANNEL_SECRET)}")
print(f"TOKEN length: {len(LINE_CHANNEL_ACCESS_TOKEN)}")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
parser = WebhookParser(LINE_CHANNEL_SECRET)

logger = logging.getLogger("uvicorn.error")

# =========================================================
# 🧩 Google Apps Script API (Config + History + Subs)
# =========================================================
BASE_URL = "https://script.google.com/macros/s/AKfycbzlvan12-CNKU97jHaKGMdD0vVJoBD13T4GGq6cFhlshAug7oEw3KjG3WSmh3F4-iN4/exec"


# ---------- small helper ----------
def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _parse_dt(s: str) -> datetime:
    """
    แปลง string → datetime แบบกันตาย
    """
    try:
        s = str(s)
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        try:
            # เผื่อ GAS ให้มาเป็น timestamp number
            return datetime.fromtimestamp(float(s))
        except Exception:
            return datetime.min


# ---------- CONFIG ----------

def write_config(device_id: str, unit: str, adj_temp: float, adj_humid: float):
    """
    POST writeConfig

    Sheet: config
    - id        = device_id (serial เครื่องวัด)
    - unit      = ชื่อ unit / ห้อง
    - adj_temp  = ค่าชดเชย temp
    - adj_humid = ค่าชดเชย humidity
    """
    payload = {
        "action": "writeConfig",
        "id": device_id,
        "unit": unit,
        "adj_temp": adj_temp,
        "adj_humid": adj_humid,
    }
    resp = requests.post(BASE_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


def get_config_by_id(device_id: str):
    """
    GET config row ตาม device_id (id)
    """
    resp = requests.get(
        BASE_URL,
        params={"action": "getConfigById", "id": device_id},
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"getConfigById({device_id}) -> {data}")
    return data


def list_devices():
    """
    GET /exec?action=listDevices
    คืน list id ทั้งหมดจาก config
    """
    resp = requests.get(
        BASE_URL,
        params={"action": "listDevices"},
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"listDevices -> {data}")
    return data


# ---------- SUBSCRIPTIONS (หลายห้องต่อ 1 device) ----------

def add_subscription(device_id: str, line_id: str):
    """
    POST addSubscription

    Sheet: subs
    - id       = device_id
    - line_id  = LINE chat id (user/group/room)
    """
    payload = {
        "action": "addSubscription",
        "id": device_id,
        "line_id": line_id,
    }
    resp = requests.post(BASE_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


def get_subscriptions_by_id(device_id: str):
    """
    GET getSubscriptionsById
    รูปแบบตอบกลับจาก GAS:
    {
      "success": true,
      "count": n,
      "data": [
        { "id": "dev1", "line_id": "...", "created_at": ... },
        ...
      ]
    }
    """
    resp = requests.get(
        BASE_URL,
        params={"action": "getSubscriptionsById", "id": device_id},
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"getSubscriptionsById({device_id}) -> {data}")
    return data


def extract_line_ids_from_subs(subs_json) -> List[str]:
    """
    ดึง list line_id ทั้งหมดจาก JSON ของ getSubscriptionsById
    """
    line_ids: List[str] = []

    if not isinstance(subs_json, dict):
        return line_ids

    if not subs_json.get("success"):
        return line_ids

    data = subs_json.get("data", [])
    if not isinstance(data, list):
        return line_ids

    for row in data:
        if isinstance(row, dict):
            lid = row.get("line_id")
            if lid:
                line_ids.append(str(lid))

    # เอา unique เผื่อมีซ้ำ
    return list(dict.fromkeys(line_ids))


# ---------- HISTORY ----------

def append_history(
    device_id: str,
    temp: float,
    humid: float,
    hic: float,
    flag: str = "OK",
    timestamp: Optional[str] = None,
):
    """
    POST appendHistory
    timestamp ถ้าไม่ส่ง = ให้ Apps Script ใส่ new Date() เอง

    Sheet: history
    - id | timestamp | temp | humid | hic | flag
    """
    payload = {
        "action": "appendHistory",
        "id": device_id,
        "temp": temp,
        "humid": humid,
        "hic": hic,
        "flag": flag,
    }
    if timestamp:
        payload["timestamp"] = timestamp

    resp = requests.post(BASE_URL, json=payload)
    resp.raise_for_status()
    return resp.json()


def get_history_by_id_sorted(device_id: str):
    """
    GET /exec?action=getHistoryByIdSorted&id=dev1
    คืน history ของ device นี้ sort ตาม timestamp (เก่า → ใหม่)
    """
    resp = requests.get(
        BASE_URL,
        params={"action": "getHistoryByIdSorted", "id": device_id},
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"getHistoryByIdSorted({device_id}) -> count={data.get('count')}")
    return data


# =========================================================
# 🌐 LINE Flex Card Builder (เมนู register / status / history)
# =========================================================
def build_main_menu_flex(register_url: str, status_url: str, history_url: str) -> dict:
    """
    สร้าง Flex Message แบบ bubble ที่มีปุ่ม:
    - ลงทะเบียน / แก้ไขอุปกรณ์
    - ดูสถานะล่าสุด
    - ดูประวัติ & กราฟ
    """
    return {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": "เครื่องวัดอุณหภูมิและความชื้นสัมพัทธ์อัตโนมมัติ",
                    "weight": "bold",
                    "size": "lg",
                    "wrap": True
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "ตั้งค่าอุปกรณ์",
                        "uri": register_url
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "ดูสถานะล่าสุด",
                        "uri": status_url
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "ดูประวัติ & กราฟ",
                        "uri": history_url
                    }
                }
            ]
        }
    }


# =========================================================
# 🌐 LINE Webhook Endpoint
# =========================================================
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_text = body.decode("utf-8")

    logger.info(f"X-Line-Signature: {signature}")
    logger.info(f"Body: {body_text}")

    if not signature:
        return PlainTextResponse("Missing signature", status_code=400)

    try:
        events = parser.parse(body_text, signature)
    except InvalidSignatureError:
        logger.exception("Invalid signature. Check LINE_CHANNEL_SECRET.")
        return PlainTextResponse("Invalid signature", status_code=400)
    except Exception as e:
        logger.exception(f"Parse error: {e}")
        return PlainTextResponse("Parse error", status_code=200)  # กัน LINE redelivery loop

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessage):

            user_text = event.message.text.strip()

            # หา LINE User ID / Group ID / Room ID สำหรับใช้เป็น line_id
            source_type = event.source.type  # "user", "group", "room"
            if source_type == "user":
                line_chat_id = event.source.user_id
            elif source_type == "group":
                line_chat_id = event.source.group_id
            elif source_type == "room":
                line_chat_id = event.source.room_id
            else:
                line_chat_id = "unknown"

            lower = user_text.lower()
            reply_message = None

            # ใช้คำสั่ง /ht ให้โชว์เมนูเดียวกัน
            if lower.startswith("/ht"):
                register_url = f"{WEB_BASE_URL}/register?line_id={line_chat_id}"
                history_url = f"{WEB_BASE_URL}/history?line_id={line_chat_id}"
                status_url = f"{WEB_BASE_URL}/status?line_id={line_chat_id}"

                contents = build_main_menu_flex(
                    register_url=register_url,
                    status_url=status_url,
                    history_url=history_url,
                )

                reply_message = FlexSendMessage(
                    alt_text="เมนูจัดการอุปกรณ์วัดอุณหภูมิ/ความชื้น",
                    contents=contents,
                )

            if reply_message:
                line_bot_api.reply_message(
                    event.reply_token,
                    reply_message
                )

    return PlainTextResponse("OK", status_code=200)


def get_current_status_by_line_id(line_id: str):
    """
    GET /exec?action=current_status&line_id=...
    คืน list device + last reading + status
    """
    resp = requests.get(
        BASE_URL,
        params={"action": "current_status", "line_id": line_id},
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"current_status({line_id}) -> {data}")
    return data


def get_history_by_line_id(line_id: str):
    """
    GET /exec?action=history&line_id=...
    คืน history ของทุก device ที่ผูกกับ line นี้ (timestamp DESC)
    """
    resp = requests.get(
        BASE_URL,
        params={"action": "history", "line_id": line_id},
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"history({line_id}) -> count={data.get('count')}")
    return data


# =========================================================
# 📝 เว็บฟอร์ม /register (GET + POST)
# =========================================================

@app.get("/register", response_class=HTMLResponse)
def register_form(
    line_id: Optional[str] = None,
    device_id: Optional[str] = None,
):
    """
    ถ้าไม่มี line_id → ไม่ให้ใช้งาน, ขึ้นข้อความว่าให้เปิดจากช่องแชท LINE
    ถ้ามี line_id:
        Stage 1: ยังไม่มี device_id → ให้กรอกแค่ device_id ก่อน (validate จาก listDevices)
        Stage 2: มี device_id แล้ว → ดึง config (ถ้ามี) มาเติม unit/adj_temp/adj_humid
    """

    # -----------------------------------------------------
    # กรณีไม่มี line_id → ไม่ให้ใช้งานฟอร์ม
    # -----------------------------------------------------
    if not line_id:
        html = """
        <!DOCTYPE html>
        <html lang="th">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>ไม่สามารถเปิดหน้าลงทะเบียนได้</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f3f4f6;
                    color: #111827;
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .card {
                    background: #ffffff;
                    border-radius: 18px;
                    padding: 24px 20px;
                    box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                    max-width: 420px;
                    width: 90%;
                    text-align: center;
                    border: 1px solid #e5e7eb;
                }
                h1 {
                    font-size: 1.4rem;
                    margin-bottom: 10px;
                }
                p {
                    font-size: 0.95rem;
                    line-height: 1.5;
                    margin: 6px 0;
                }
                .badge {
                    display: inline-block;
                    padding: 4px 10px;
                    border-radius: 999px;
                    background: #e0f2fe;
                    color: #0369a1;
                    font-size: 0.78rem;
                    margin-bottom: 10px;
                }
                .hint {
                    margin-top: 10px;
                    font-size: 0.85rem;
                    color: #6b7280;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="badge">LINE Device Config</div>
                <h1>ไม่สามารถใช้งานหน้านี้ได้โดยตรง</h1>
                <p>กรุณากลับไปที่ห้องแชท LINE ของบอทนี้</p>
                <p>แล้วพิมพ์คำสั่ง <b>/register</b> จากนั้นเปิดลิงก์ที่บอทส่งมาอีกครั้ง</p>
                <p class="hint">
                    ระบบต้องใช้ข้อมูลห้องแชทจาก LINE เพื่อเชื่อมกับอุปกรณ์และส่งแจ้งเตือนกลับได้อย่างถูกต้อง
                </p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    # -----------------------------------------------------
    # มี line_id แล้ว
    # -----------------------------------------------------

    # --- Stage 1: ยังไม่มี device_id → ให้กรอก device_id ก่อน ---
    if not device_id:
        html = f"""
        <!DOCTYPE html>
        <html lang="th">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>เลือก Device ID</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f3f4f6;
                    color: #111827;
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 16px;
                }}
                .card {{
                    background: #ffffff;
                    border-radius: 18px;
                    padding: 24px 20px;
                    box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                    max-width: 440px;
                    width: 100%;
                    border: 1px solid #e5e7eb;
                }}
                h1 {{
                    font-size: 1.35rem;
                    margin-bottom: 8px;
                }}
                p {{
                    font-size: 0.95rem;
                    line-height: 1.5;
                    margin: 4px 0;
                }}
                label {{
                    display: block;
                    margin-top: 14px;
                    font-size: 0.9rem;
                }}
                input[type="text"] {{
                    width: 100%;
                    padding: 10px 12px;
                    margin-top: 6px;
                    border-radius: 10px;
                    border: 1px solid #d1d5db;
                    background: #f9fafb;
                    color: #111827;
                    font-size: 0.95rem;
                    box-sizing: border-box;
                }}
                input[type="text"]:focus {{
                    outline: none;
                    border-color: #38bdf8;
                    box-shadow: 0 0 0 1px #38bdf8;
                    background: #ffffff;
                }}
                button {{
                    margin-top: 20px;
                    width: 100%;
                    padding: 10px 14px;
                    border-radius: 999px;
                    border: none;
                    font-size: 0.98rem;
                    font-weight: 600;
                    background: linear-gradient(135deg,#38bdf8,#22c55e);
                    color: #ffffff;
                    cursor: pointer;
                }}
                button:active {{
                    transform: scale(0.98);
                }}
                .line-id {{
                    margin-top: 4px;
                    font-size: 0.82rem;
                    color: #6b7280;
                    word-break: break-all;
                }}
                .pill {{
                    display: inline-block;
                    padding: 3px 8px;
                    border-radius: 999px;
                    background: #e0f2fe;
                    color: #0369a1;
                    font-size: 0.75rem;
                    margin-bottom: 4px;
                }}

                .loading-backdrop {{
                    position: fixed;
                    inset: 0;
                    background: rgba(15, 23, 42, 0.45);
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    z-index: 9999;
                    backdrop-filter: blur(3px);
                    transition: opacity 0.15s ease-out;
                    opacity: 1;
                }}
                .loading-backdrop.hidden {{
                    opacity: 0;
                    pointer-events: none;
                }}
                .loading-box {{
                    background: rgba(15, 23, 42, 0.9);
                    padding: 16px 18px;
                    border-radius: 16px;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    gap: 10px;
                    min-width: 160px;
                }}
                .loading-spinner {{
                    width: 32px;
                    height: 32px;
                    border-radius: 999px;
                    border: 3px solid rgba(148, 163, 184, 0.5);
                    border-top-color: #38bdf8;
                    animation: spin 0.7s linear infinite;
                }}
                .loading-text {{
                    font-size: 0.9rem;
                    color: #e5e7eb;
                }}
                @keyframes spin {{
                    to {{ transform: rotate(360deg); }}
                }}
            </style>
            <script>
            function showGlobalLoading(label) {{
                var overlay = document.getElementById('global-loading');
                if (!overlay) return;
                var textEl = overlay.querySelector('.loading-text');
                if (textEl) {{
                    textEl.textContent = label || 'กำลังโหลด...';
                }}
                overlay.classList.remove('hidden');
            }}

            function hideGlobalLoading() {{
                var overlay = document.getElementById('global-loading');
                if (!overlay) return;
                overlay.classList.add('hidden');
            }}

            window.addEventListener('pageshow', function() {{
                hideGlobalLoading();
            }});
            </script>
        </head>
        <body>
            <div id="global-loading" class="loading-backdrop hidden">
                <div class="loading-box">
                    <div class="loading-spinner"></div>
                    <div class="loading-text">กำลังโหลด...</div>
                </div>
            </div>

            <div class="card">
                <div class="pill">Step 1 / 2</div>
                <h1>Device ID (serial เครื่องวัด):</h1>

                <form method="get" action="/register"
                      onsubmit="showGlobalLoading('กำลังตรวจสอบ Device ID...');">
                    <label>
                        <input type="text" name="device_id" required placeholder="เช่น HTxxx" />
                    </label>
                    <input type="hidden" name="line_id" value="{line_id}" />
                    <button type="submit">ถัดไป</button>
                </form>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    # --- Stage 2: มี device_id แล้ว → validate กับ listDevices ก่อน ---
    try:
        dev_list_json = list_devices()
        if not (isinstance(dev_list_json, dict) and dev_list_json.get("success")):
            valid_ids = []
        else:
            valid_ids = [str(x) for x in dev_list_json.get("data", [])]
    except Exception as e:
        logger.exception("Error calling listDevices")
        valid_ids = []

    if valid_ids and device_id not in valid_ids:
        # device_id ไม่อยู่ใน listDevices → ขึ้น error card
        html = f"""
        <!DOCTYPE html>
        <html lang="th">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Device ID ไม่ถูกต้อง</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f3f4f6;
                    color: #111827;
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 16px;
                }}
                .card {{
                    background: #ffffff;
                    border-radius: 18px;
                    padding: 22px 18px 20px;
                    box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                    max-width: 460px;
                    width: 100%;
                    border: 1px solid #fecaca;
                }}
                h1 {{
                    font-size: 1.3rem;
                    margin-bottom: 6px;
                }}
                p {{
                    font-size: 0.94rem;
                    line-height: 1.5;
                    margin: 4px 0;
                }}
                .badge {{
                    display: inline-block;
                    padding: 3px 9px;
                    border-radius: 999px;
                    background: #fee2e2;
                    color: #b91c1c;
                    font-size: 0.78rem;
                    margin-bottom: 6px;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="badge">Device Not Found</div>
                <h1>"{device_id}" ไม่อยู่ในรายการอุปกรณ์ที่ระบบรู้จัก</h1>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    # --- ถ้า device_id อยู่ใน list แล้ว → ลองดึง config มาเติมค่า ---
    unit_value = ""
    adj_temp_value = "0.0"
    adj_humid_value = "0.0"

    try:
        cfg = get_config_by_id(device_id)
        if isinstance(cfg, dict) and cfg.get("success") and cfg.get("count", 0) > 0:
            row = cfg["data"][0]
            unit_value = str(row.get("unit", "") or "")
            adj_temp_value = f"{_safe_float(row.get('adj_temp', 0.0)):.1f}"
            adj_humid_value = f"{_safe_float(row.get('adj_humid', 0.0)):.1f}"
    except Exception as e:
        logger.exception("Error fetching config for register_form")

    # ฟอร์มขั้นที่ 2 (ตั้งค่า Unit + Adj temp/humid)
    html = f"""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>ตั้งค่าอุปกรณ์</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #f3f4f6;
                color: #111827;
                min-height: 100vh;
                margin: 0;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 16px;
            }}
            .card {{
                background: #ffffff;
                border-radius: 20px;
                padding: 22px 18px 26px;
                box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                max-width: 460px;
                width: 100%;
                border: 1px solid #e5e7eb;
            }}
            h1 {{
                font-size: 1.35rem;
                margin-bottom: 4px;
            }}
            p {{
                font-size: 0.94rem;
                line-height: 1.5;
                margin: 4px 0;
            }}
            .sub {{
                font-size: 0.85rem;
                color: #6b7280;
                margin-bottom: 10px;
            }}
            .pill {{
                display: inline-block;
                padding: 3px 9px;
                border-radius: 999px;
                background: #e0f2fe;
                color: #0369a1;
                font-size: 0.78rem;
                margin-bottom: 6px;
            }}
            .device-pill {{
                display: inline-block;
                padding: 4px 10px;
                border-radius: 999px;
                background: #f9fafb;
                border: 1px solid #e5e7eb;
                font-size: 0.8rem;
                margin: 6px 0 8px;
            }}
            .line-id {{
                font-size: 0.8rem;
                color: #6b7280;
                word-break: break-all;
            }}
            label {{
                display: block;
                margin-top: 14px;
                font-size: 0.9rem;
            }}
            input[type="text"],
            select {{
                width: 100%;
                padding: 10px 12px;
                margin-top: 6px;
                border-radius: 10px;
                border: 1px solid #d1d5db;
                background: #f9fafb;
                color: #111827;
                font-size: 0.95rem;
                box-sizing: border-box;
            }}
            input[type="text"]:focus,
            select:focus {{
                outline: none;
                border-color: #38bdf8;
                box-shadow: 0 0 0 1px #38bdf8;
                background: #ffffff;
            }}
            .input-row {{
                display: flex;
                gap: 10px;
            }}
            .input-row > div {{
                flex: 1;
            }}
            button {{
                margin-top: 22px;
                width: 100%;
                padding: 11px 16px;
                border-radius: 999px;
                border: none;
                font-size: 0.98rem;
                font-weight: 600;
                background: linear-gradient(135deg,#38bdf8,#22c55e);
                color: #ffffff;
                cursor: pointer;
            }}
            button:active {{
                transform: scale(0.98);
            }}
            .note {{
                margin-top: 10px;
                font-size: 0.82rem;
                color: #6b7280;
            }}

            .loading-backdrop {{
                position: fixed;
                inset: 0;
                background: rgba(15, 23, 42, 0.45);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 9999;
                backdrop-filter: blur(3px);
                transition: opacity 0.15s ease-out;
                opacity: 1;
            }}
            .loading-backdrop.hidden {{
                opacity: 0;
                pointer-events: none;
            }}
            .loading-box {{
                background: rgba(15, 23, 42, 0.9);
                padding: 16px 18px;
                border-radius: 16px;
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 10px;
                min-width: 160px;
            }}
            .loading-spinner {{
                width: 32px;
                height: 32px;
                border-radius: 999px;
                border: 3px solid rgba(148, 163, 184, 0.5);
                border-top-color: #38bdf8;
                animation: spin 0.7s linear infinite;
            }}
            .loading-text {{
                font-size: 0.9rem;
                color: #e5e7eb;
            }}
            @keyframes spin {{
                to {{ transform: rotate(360deg); }}
            }}
        </style>
        <script>
        function showGlobalLoading(label) {{
            var overlay = document.getElementById('global-loading');
            if (!overlay) return;
            var textEl = overlay.querySelector('.loading-text');
            if (textEl) {{
                textEl.textContent = label || 'กำลังโหลด...';
            }}
            overlay.classList.remove('hidden');
        }}

        function hideGlobalLoading() {{
            var overlay = document.getElementById('global-loading');
            if (!overlay) return;
            overlay.classList.add('hidden');
        }}

        window.addEventListener('pageshow', function() {{
            hideGlobalLoading();
        }});

        function onSubmitForm(form) {{
            showGlobalLoading('กำลังบันทึกการตั้งค่า...');

            var btn = form.querySelector('button[type="submit"]');
            if (btn) {{
                btn.disabled = true;
                btn.innerText = 'กำลังบันทึก...';
            }}

            var inputs = form.querySelectorAll('input, select');
            inputs.forEach(function(el) {{
                // el.readOnly = true;
            }});

            return true;
        }}

        function populateAdjSelect(selectId, defaultValue) {{
            var select = document.getElementById(selectId);
            if (!select) return;

            var min = -5.0;
            var max = 5.0;
            var step = 0.1;
            var def = parseFloat(defaultValue);

            for (var value = min; value <= max + 1e-9; value += step) {{
                var option = document.createElement('option');
                option.value = value.toFixed(1);
                option.textContent = value.toFixed(1);
                if (Math.abs(value - def) < 1e-9) {{
                    option.selected = true;
                }}
                select.appendChild(option);
            }}
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            populateAdjSelect('adj_temp', {adj_temp_value});
            populateAdjSelect('adj_humid', {adj_humid_value});
        }});
        </script>
    </head>
    <body>
        <div id="global-loading" class="loading-backdrop hidden">
            <div class="loading-box">
                <div class="loading-spinner"></div>
                <div class="loading-text">กำลังโหลด...</div>
            </div>
        </div>

        <div class="card">
            <div class="pill">Step 2 / 2</div>
            <h1>ตั้งค่าอุปกรณ์</h1>

            <div class="device-pill">Device ID: <b>{device_id}</b></div>

            <form method="post" action="/register" onsubmit="return onSubmitForm(this);">
                <input type="hidden" name="device_id" value="{device_id}" />
                <input type="hidden" name="line_chat_id" value="{line_id}" />

                <label>
                    หน่วย:
                    <input type="text" name="unit_name" value="{unit_value}" required placeholder="เช่น หน่วยฝึกxxx" />
                </label>

                <div class="input-row">
                    <div>
                        <label>
                            ชดเชยอุณหภูมิ(°C):
                            <select name="adj_temp" id="adj_temp" required></select>
                        </label>
                    </div>
                    <div>
                        <label>
                            ชดเชยความชื้น(%RH):
                            <select name="adj_humid" id="adj_humid" required></select>
                        </label>
                    </div>
                </div>

                <button type="submit">อัปเดตการตั้งค่า</button>

                <p class="note">
                    ** ค่าชดเชย เช่น ถ้าเซนเซอร์อ่านอุณหภูมิต่ำกว่าจริง 0.1°C ให้ใส่ <b>+0.1</b> เป็นต้น 
                </p>
            </form>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/register", response_class=HTMLResponse)
def register_submit(
    device_id: str = Form(...),
    unit_name: str = Form(...),
    adj_temp: float = Form(...),
    adj_humid: float = Form(...),
    line_chat_id: str = Form(...),
):
    """
    บันทึก:
    1) config ลง Google Sheet (config sheet)
    2) subscription ลงชีต subs
    """
    try:
        cfg_result = write_config(
            device_id=device_id,
            unit=unit_name,
            adj_temp=adj_temp,
            adj_humid=adj_humid,
        )
    except Exception as e:
        logger.exception("Error in write_config")
        cfg_result = {"error": str(e)}

    try:
        subs_result = add_subscription(
            device_id=device_id,
            line_id=line_chat_id,
        )
    except Exception as e:
        logger.exception("Error in add_subscription")
        subs_result = {"error": str(e)}

    result_obj = {
        "config_result": cfg_result,
        "subscription_result": subs_result,
    }
    status_html = f"<pre>{json.dumps(result_obj, ensure_ascii=False, indent=2)}</pre>"

    html = f"""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>บันทึกสำเร็จ</title>
        <style>
        * {{
            box-sizing: border-box;
        }}

            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #f3f4f6;
                color: #111827;
                min-height: 100vh;
                margin: 0;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 16px;
            }}
            .card {{
                background: #ffffff;
                border-radius: 18px;
                padding: 22px 18px 24px;
                box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                max-width: 460px;
                width: 100%;
                border: 1px solid #bbf7d0;
            }}
            h1 {{
                font-size: 1.35rem;
                margin-bottom: 6px;
            }}
            p {{
                font-size: 0.94rem;
                line-height: 1.5;
                margin: 4px 0;
            }}
            pre {{
                background: #f9fafb;
                border-radius: 10px;
                padding: 10px;
                font-size: 0.76rem;
                overflow-x: auto;
                border: 1px solid #e5e7eb;
                margin-top: 14px;
            }}
            a {{
                display: inline-block;
                margin-top: 14px;
                font-size: 0.9rem;
                color: #0369a1;
                text-decoration: none;
            }}
            a:active {{
                transform: scale(0.98);
            }}
            .badge {{
                display: inline-block;
                padding: 3px 9px;
                border-radius: 999px;
                background: #dcfce7;
                color: #15803d;
                font-size: 0.78rem;
                margin-bottom: 6px;
            }}

            .loading-backdrop {{
                position: fixed;
                inset: 0;
                background: rgba(15, 23, 42, 0.45);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 9999;
                backdrop-filter: blur(3px);
                transition: opacity 0.15s ease-out;
                opacity: 1;
            }}
            .loading-backdrop.hidden {{
                opacity: 0;
                pointer-events: none;
            }}
            .loading-box {{
                background: rgba(15, 23, 42, 0.9);
                padding: 16px 18px;
                border-radius: 16px;
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 10px;
                min-width: 160px;
            }}
            .loading-spinner {{
                width: 32px;
                height: 32px;
                border-radius: 999px;
                border: 3px solid rgba(148, 163, 184, 0.5);
                border-top-color: #38bdf8;
                animation: spin 0.7s linear infinite;
            }}
            .loading-text {{
                font-size: 0.9rem;
                color: #e5e7eb;
            }}
            @keyframes spin {{
                to {{ transform: rotate(360deg); }}
            }}
        </style>
        <script>
        function showGlobalLoading(label) {{
            var overlay = document.getElementById('global-loading');
            if (!overlay) return;
            var textEl = overlay.querySelector('.loading-text');
            if (textEl) {{
                textEl.textContent = label || 'กำลังโหลด...';
            }}
            overlay.classList.remove('hidden');
        }}

        function hideGlobalLoading() {{
            var overlay = document.getElementById('global-loading');
            if (!overlay) return;
            overlay.classList.add('hidden');
        }}

        window.addEventListener('pageshow', function() {{
            hideGlobalLoading();
        }});
        </script>
    </head>
    <body>
        <div id="global-loading" class="loading-backdrop hidden">
            <div class="loading-box">
                <div class="loading-spinner"></div>
                <div class="loading-text">กำลังโหลด...</div>
            </div>
        </div>

        <div class="card">
            <div class="badge">Saved</div>
            <h1>บันทึกการตั้งค่าเรียบร้อย</h1>
            <p>Device ID: <b>{device_id}</b></p>
            <p>Unit: <b>{unit_name}</b></p>
            <p>Adj Temp: <b>{adj_temp}</b> °C</p>
            <p>Adj Humid: <b>{adj_humid}</b> %RH</p>
            <p>LINE Chat ID: <b>{line_chat_id}</b></p>

            {status_html}

            <a href="/register?line_id={line_chat_id}&device_id={device_id}"
               onclick="showGlobalLoading('กำลังเปิดหน้าแก้ไข...');">
                ⬅ กลับไปหน้าแก้ไขอุปกรณ์นี้
            </a>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# =========================================================
# 📊 หน้า /history (GET) – dropdown + graph + table + pagination
# =========================================================

@app.get("/history", response_class=HTMLResponse)
def history_page(
    line_id: Optional[str] = None,
    device_id: Optional[str] = None,
    page: int = 1,
):
    """
    แสดงประวัติการวัด:
    - ต้องมี line_id (เปิดจาก LINE เท่านั้น)
    - ใช้ current_status(line_id) หา device list ของห้องนี้ (1 call)
    - ใช้ history(line_id) ดึง history ของทุก device ของห้องนี้ (1 call)
    - dropdown เลือก device
    - default = device ที่อยู่บนสุดจาก current_status (ซึ่ง sort online ก่อนให้แล้ว)
    - table + graph + pagination (200 แถว/หน้า, ล่าสุดก่อน)
    """
    if not line_id:
        # เหมือน /register กรณีไม่มี line_id
        html = """
        <!DOCTYPE html>
        <html lang="th">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>ไม่สามารถเปิดหน้าประวัติได้</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f3f4f6;
                    color: #111827;
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .card {
                    background: #ffffff;
                    border-radius: 18px;
                    padding: 24px 20px;
                    box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                    max-width: 420px;
                    width: 90%;
                    text-align: center;
                    border: 1px solid #e5e7eb;
                }
                h1 {
                    font-size: 1.4rem;
                    margin-bottom: 10px;
                }
                p {
                    font-size: 0.95rem;
                    line-height: 1.5;
                    margin: 6px 0;
                }
                .badge {
                    display: inline-block;
                    padding: 4px 10px;
                    border-radius: 999px;
                    background: #e0f2fe;
                    color: #0369a1;
                    font-size: 0.78rem;
                    margin-bottom: 10px;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="badge">LINE History</div>
                <h1>ไม่สามารถเปิดหน้าประวัติได้โดยตรง</h1>
                <p>กรุณากลับไปที่ห้องแชท LINE แล้วพิมพ์คำสั่ง <b>/history</b></p>
                <p>แล้วเปิดลิงก์ที่บอทส่งมาอีกครั้ง</p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    # 1) ดึง current_status ของ line_id นี้ → ได้ device list + lastupdate + status
    try:
        status_json = get_current_status_by_line_id(line_id)
        if not (isinstance(status_json, dict) and status_json.get("success")):
            devices_info = []
        else:
            devices_info = status_json.get("data", [])
    except Exception as e:
        logger.exception("Error calling current_status in /history")
        devices_info = []

    if not devices_info:
        html = f"""
        <!DOCTYPE html>
        <html lang="th">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>ยังไม่มีอุปกรณ์ที่ผูกกับห้องนี้</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f3f4f6;
                    color: #111827;
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 16px;
                }}
                .card {{
                    background: #ffffff;
                    border-radius: 18px;
                    padding: 24px 20px;
                    box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                    max-width: 440px;
                    width: 100%;
                    border: 1px solid #e5e7eb;
                    text-align: center;
                }}
                h1 {{
                    font-size: 1.35rem;
                    margin-bottom: 8px;
                }}
                p {{
                    font-size: 0.95rem;
                    line-height: 1.5;
                    margin: 4px 0;
                }}
                .badge {{
                    display: inline-block;
                    padding: 3px 9px;
                    border-radius: 999px;
                    background: #fee2e2;
                    color: #b91c1c;
                    font-size: 0.78rem;
                    margin-bottom: 6px;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="badge">No Devices</div>
                <h1>ยังไม่มีอุปกรณ์ที่ผูกกับห้องแชทนี้</h1>
                <p>กรุณาใช้คำสั่ง <b>/register</b> ในห้อง LINE นี้</p>
                <p>เพื่อผูก Device ID กับห้องแชท แล้วจึงกลับมาดูประวัติอีกครั้ง</p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    device_ids_only = [str(d.get("id")) for d in devices_info if d.get("id")]

    # เลือก device ปัจจุบัน
    if device_id and device_id in device_ids_only:
        selected_device = device_id
    else:
        selected_device = device_ids_only[0]

    # 2) ดึง history ของ line นี้ครั้งเดียว แล้ว filter ตาม device
    try:
        hist_json = get_history_by_line_id(line_id)
        if isinstance(hist_json, dict) and hist_json.get("success"):
            all_hist = hist_json.get("data", [])
        else:
            all_hist = []
    except Exception as e:
        logger.exception("Error calling history(line_id) in /history")
        all_hist = []

    # history จาก GAS เป็น timestamp DESC (ใหม่สุด → เก่าสุด)
    hist_selected = [r for r in all_hist if str(r.get("id")) == selected_device]

    # pagination (200 แถว/หน้า)
    per_page = 200
    total = len(hist_selected)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_rows = hist_selected[start:end]

    # เตรียม data สำหรับ Chart.js (ให้กราฟเป็นเก่า→ใหม่ภายในหน้า)
    chart_rows = list(reversed(page_rows))
    labels = [
        format_ts_th(r.get("timestamp", "")) if r.get("timestamp") else ""
        for r in chart_rows
    ]

    temps = [_safe_float(r.get("temp")) for r in chart_rows]
    humids = [_safe_float(r.get("humid")) for r in chart_rows]
    hics = [_safe_float(r.get("hic")) for r in chart_rows]

    chart_payload = {
        "labels": labels,
        "temp": temps,
        "humid": humids,
        "hic": hics,
    }
    chart_json = json.dumps(chart_payload, ensure_ascii=False)

    # dropdown options
    options_html = ""
    for d in devices_info:
        did = str(d.get("id"))
        if not did:
            continue
        sel = "selected" if did == selected_device else ""
        status = d.get("status", "")
        badge = "🟢" if status == "online" else "⚪️"
        options_html += f'<option value="{did}" {sel}>{badge} {did}</option>'

    # pagination html
    pagination_html = ""
    if total_pages > 1:
        pagination_html += '<div class="pagination">'
        if page > 1:
            prev_page = page - 1
            pagination_html += f'<a href="/history?line_id={line_id}&device_id={selected_device}&page={prev_page}">‹ ก่อนหน้า</a>'
        pagination_html += f'<span>หน้า {page} / {total_pages}</span>'
        if page < total_pages:
            next_page = page + 1
            pagination_html += f'<a href="/history?line_id={line_id}&device_id={selected_device}&page={next_page}">ถัดไป ›</a>'
        pagination_html += "</div>"

    # table rows (page_rows เป็นใหม่สุด→เก่าสุด)
    table_rows_html = ""
    for r in page_rows:
        ts_raw = r.get("timestamp", "")
        ts = format_ts_th(ts_raw) if ts_raw else ""
        temp = _safe_float(r.get("temp"))
        humid = _safe_float(r.get("humid"))
        hic = _safe_float(r.get("hic"))
        flag = r.get("flag", "")
        table_rows_html += f"""
        <tr>
            <td>{ts}</td>
            <td>{temp:.1f}</td>
            <td>{humid:.1f}</td>
            <td>{hic:.1f}</td>
            <td>{flag}</td>
        </tr>
        """

    # หา status ของ device ที่เลือก
    selected_info = next((d for d in devices_info if str(d.get("id")) == selected_device), None)
    sel_status = selected_info.get("status") if selected_info else "-"
    raw_lastupdate = selected_info.get("lastupdate") if selected_info else "-"
    sel_lastupdate = format_ts_th(raw_lastupdate) if raw_lastupdate not in (None, "-", "") else "-"

    html = f"""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>History - {selected_device}</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #f3f4f6;
                color: #111827;
                margin: 0;
                padding: 16px;
            }}
            .container {{
                max-width: 1000px;
                margin: 0 auto;
            }}
            .card {{
                background: #ffffff;
                border-radius: 18px;
                padding: 18px 16px 20px;
                box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                border: 1px solid #e5e7eb;
                margin-bottom: 16px;
            }}
            .header {{
                display: flex;
                flex-wrap: wrap;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
            }}
            h1 {{
                font-size: 1.3rem;
                margin: 0;
            }}
            .sub {{
                font-size: 0.86rem;
                color: #6b7280;
            }}
            select {{
                padding: 8px 10px;
                border-radius: 999px;
                border: 1px solid #d1d5db;
                background: #f9fafb;
                font-size: 0.9rem;
            }}
            .info-line {{
                font-size: 0.8rem;
                color: #6b7280;
                margin-top: 4px;
            }}
            canvas {{
                max-height: 280px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
                font-size: 0.85rem;
            }}
            th, td {{
                border-bottom: 1px solid #e5e7eb;
                padding: 6px 8px;
                text-align: left;
            }}
            th {{
                background: #f9fafb;
                font-weight: 600;
            }}
            .pagination {{
                display: flex;
                justify-content: flex-end;
                align-items: center;
                gap: 8px;
                margin-top: 8px;
                font-size: 0.85rem;
            }}
            .pagination a {{
                text-decoration: none;
                color: #0369a1;
                padding: 3px 8px;
                border-radius: 999px;
                background: #e0f2fe;
            }}
            .pagination span {{
                color: #4b5563;
            }}

            .loading-backdrop {{
                position: fixed;
                inset: 0;
                background: rgba(15, 23, 42, 0.45);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 9999;
                backdrop-filter: blur(3px);
                transition: opacity 0.15s ease-out;
                opacity: 1;
            }}
            .loading-backdrop.hidden {{
                opacity: 0;
                pointer-events: none;
            }}
            .loading-box {{
                background: rgba(15, 23, 42, 0.9);
                padding: 16px 18px;
                border-radius: 16px;
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 10px;
                min-width: 160px;
            }}
            .loading-spinner {{
                width: 32px;
                height: 32px;
                border-radius: 999px;
                border: 3px solid rgba(148, 163, 184, 0.5);
                border-top-color: #38bdf8;
                animation: spin 0.7s linear infinite;
            }}
            .loading-text {{
                font-size: 0.9rem;
                color: #e5e7eb;
            }}
            @keyframes spin {{
                to {{ transform: rotate(360deg); }}
            }}
        </style>
        <script>
        function showGlobalLoading(label) {{
            var overlay = document.getElementById('global-loading');
            if (!overlay) return;
            var textEl = overlay.querySelector('.loading-text');
            if (textEl) {{
                textEl.textContent = label || 'กำลังโหลด...';
            }}
            overlay.classList.remove('hidden');
        }}

        function hideGlobalLoading() {{
            var overlay = document.getElementById('global-loading');
            if (!overlay) return;
            overlay.classList.add('hidden');
        }}

        window.addEventListener('pageshow', function() {{
            hideGlobalLoading();
        }});
        </script>
    </head>
    <body>
        <div id="global-loading" class="loading-backdrop hidden">
            <div class="loading-box">
                <div class="loading-spinner"></div>
                <div class="loading-text">กำลังโหลด...</div>
            </div>
        </div>

        <div class="container">
            <div class="card">
                <div class="header">
                    <div>
                        <h1>History &amp; Graph</h1>
                    </div>
                    <form method="get" action="/history"
                          onsubmit="showGlobalLoading('กำลังโหลดประวัติ...');">
                        <input type="hidden" name="line_id" value="{line_id}" />
                        <label style="font-size:0.85rem; margin-right:4px;">Device:</label>
                        <select name="device_id" onchange="this.form.submit()">
                            {options_html}
                        </select>
                        <div class="info-line">
                            Status: <b>{sel_status}</b> | Last update: <b>{sel_lastupdate}</b>
                        </div>
                        <input type="hidden" name="page" value="1" />
                    </form>
                </div>
            </div>

            <div class="card">
                <canvas id="historyChart"></canvas>
            </div>

            <div class="card">
                <table>
                    <thead>
                        <tr>
                            <th>Timestamp</th>
                            <th>Temp (°C)</th>
                            <th>Humid (%RH)</th>
                            <th>HIC (°C)</th>
                            <th>Flag</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows_html}
                    </tbody>
                </table>
                {pagination_html}
            </div>
        </div>

        <script>
        const chartData = {chart_json};
        const ctx = document.getElementById('historyChart').getContext('2d');

        const chart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: chartData.labels,
                datasets: [
                    {{
                        label: 'Temp (°C)',
                        data: chartData.temp,
                        yAxisID: 'y',
                        tension: 0.25
                    }},
                    {{
                        label: 'Humid (%RH)',
                        data: chartData.humid,
                        yAxisID: 'y1',
                        tension: 0.25
                    }},
                    {{
                        label: 'HIC (°C)',
                        data: chartData.hic,
                        yAxisID: 'y',
                        borderDash: [4, 3],
                        tension: 0.25
                    }},
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                interaction: {{
                    mode: 'index',
                    intersect: false,
                }},
                plugins: {{
                    legend: {{
                        position: 'top',
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: function(context) {{
                                const label = context.dataset.label || '';
                                const value = context.parsed.y;
                                return label + ': ' + value.toFixed(1);
                            }}
                        }}
                    }}
                }},
                scales: {{
                    y: {{
                        type: 'linear',
                        position: 'left',
                        title: {{
                            display: true,
                            text: 'Temp / HIC (°C)'
                        }}
                    }},
                    y1: {{
                        type: 'linear',
                        position: 'right',
                        grid: {{
                            drawOnChartArea: false,
                        }},
                        title: {{
                            display: true,
                            text: 'Humid (%)'
                        }}
                    }}
                }}
            }}
        }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# =========================================================
# 📡 API: POST /history (sensor → Google Sheet + push LINE)
# =========================================================

class HistoryIn(BaseModel):
    id: str          # ตรงนี้คือ device_id (serial เครื่องวัด)
    temp: float
    humid: float
    hic: float
    flag: str = "OK"
    timestamp: Optional[str] = None  # ถ้าไม่ส่ง ให้ App Script เติมเองได้


@app.post("/history")
async def post_history(data: HistoryIn):
    """
    API สำหรับ device ส่งค่าเข้า (ไม่ใช่หน้าเว็บ)

    เงื่อนไขเพิ่ม:
    - LINE noti จะส่งเฉพาะกรณี timestamp มีนาที = 00 (เช่น 01:00, 02:00, 13:00)
    - ข้อความบรรทัดแรกใช้ Unit จาก config แทน Device
    """
    device_id = data.id

    # 1) บันทึก History ลง Google Sheet (บันทึกทุกครั้ง)
    try:
        gs_result = append_history(
            device_id=device_id,
            temp=data.temp,
            humid=data.humid,
            hic=data.hic,
            flag=data.flag,
            timestamp=data.timestamp,
        )
    except Exception as e:
        logger.exception("Error when calling append_history")
        return {
            "status": "error",
            "message": f"append_history failed: {e}",
        }

    # 2.1) เช็คว่าเวลานาที = 00 ไหม ถ้าไม่ใช่จะไม่ส่ง LINE noti
    notify_allowed = True
    if data.timestamp:
        dt = _parse_dt(data.timestamp)
        if dt != datetime.min and dt.minute != 0:
            notify_allowed = False

    # 2.2) ดึง unit จาก config (เอาไปใช้ในข้อความ noti)
    unit_name = device_id  # fallback
    try:
        cfg = get_config_by_id(device_id)
        if isinstance(cfg, dict) and cfg.get("success") and cfg.get("count", 0) > 0:
            row = cfg["data"][0]
            unit_name = str(row.get("unit") or device_id)
    except Exception as e:
        logger.exception("Error fetching config in post_history")

    # 3) ดึง subs ตาม device_id (อาจมีหลายห้อง)
    try:
        subs_json = get_subscriptions_by_id(device_id)
        line_ids = extract_line_ids_from_subs(subs_json)
    except Exception as e:
        logger.exception("Error when calling get_subscriptions_by_id")
        line_ids = []

    flag_map = {
        "white":  {
            "water": "อย่างน้อย 0.5 ลิตร",
            "rest": "50/10 นาที"
        },
        "green": {
            "water": "อย่างน้อย 0.5 ลิตร",
            "rest": "50/10 นาที"
        },
        "yellow": {
            "water": "อย่างน้อย 1 ลิตร",
            "rest": "45/15 นาที"
        },
        "red": {
            "water": "อย่างน้อย 1 ลิตร",
            "rest": "30/30 นาที"
        },
        "black": {
            "water": "อย่างน้อย 1 ลิตร",
            "rest": "20/40 นาที"
        }
    }

    flag_th = {
        "white": "⚪⚪⚪",
        "green": "🟢🟢🟢",
        "yellow": "🟡🟡🟡",
        "red": "🔴🔴🔴",
        "black": "⚫⚫⚫"
    }

    msg_lines = [
        f"หน่วย: {unit_name}",
        f"🌡อุณหภูมิ: {data.temp:.1f} °C",
        f"💧ความชื้น: {data.humid:.1f} %RH",
        f"-สัญญาณธงสี: {flag_th.get(data.flag, data.flag)}",
        f"-รู้สึกเหมือน: {data.hic:.1f} °C",
        f"-ฝึก/พัก: {flag_map.get(data.flag, {{}}).get('rest', '-')}",
        f"-ดื่มน้ำ: {flag_map.get(data.flag, {{}}).get('water', '-')}",
    ]

    msg_text = "\n".join(msg_lines)

    # 4) push LINE ไปทุก line_id (เฉพาะเวลานาที = 00)
    push_results = []
    if not notify_allowed:
        push_results.append("Skip LINE push: minute != 00")
    else:
        if line_ids:
            for lid in line_ids:
                try:
                    line_bot_api.push_message(
                        lid,
                        TextSendMessage(text=msg_text)
                    )
                    push_results.append(f"OK:{lid}")
                except Exception as e:
                    logger.exception("Error when pushing LINE message")
                    push_results.append(f"ERR:{lid}:{e}")
        else:
            push_results.append("No line_id subscribed; skip LINE push")

    return {
        "status": "ok",
        "google_sheet": gs_result,
        "line_push_results": push_results,
    }


@app.get("/status", response_class=HTMLResponse)
def status_page(line_id: Optional[str] = None):
    """
    แสดงสถานะล่าสุดของทุกอุปกรณ์ที่ผูกกับ line_id นี้
    - ใช้ current_status(line_id) ดึงข้อมูลล่าสุด
    - โชว์การ์ดสวย ๆ แยกตาม device
    """
    # ถ้าไม่มี line_id → ไม่ให้เปิดตรง ๆ
    if not line_id:
        html = """
        <!DOCTYPE html>
        <html lang="th">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>ไม่สามารถเปิดหน้าสถานะได้</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f3f4f6;
                    color: #111827;
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 16px;
                }
                .card {
                    background: #ffffff;
                    border-radius: 18px;
                    padding: 24px 20px;
                    box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                    max-width: 420px;
                    width: 100%;
                    text-align: center;
                    border: 1px solid #e5e7eb;
                }
                h1 {
                    font-size: 1.4rem;
                    margin-bottom: 10px;
                }
                p {
                    font-size: 0.95rem;
                    line-height: 1.5;
                    margin: 6px 0;
                }
                .badge {
                    display: inline-block;
                    padding: 4px 10px;
                    border-radius: 999px;
                    background: #e0f2fe;
                    color: #0369a1;
                    font-size: 0.78rem;
                    margin-bottom: 10px;
                }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="badge">LINE Status</div>
                <h1>ไม่สามารถเปิดหน้าสถานะได้โดยตรง</h1>
                <p>กรุณากลับไปที่ห้องแชท LINE แล้วพิมพ์คำสั่ง <b>/status</b></p>
                <p>แล้วเปิดลิงก์ที่บอทส่งมาอีกครั้ง</p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    # ดึง current_status ของ line นี้
    try:
        status_json = get_current_status_by_line_id(line_id)
        if not (isinstance(status_json, dict) and status_json.get("success")):
            devices_info = []
        else:
            devices_info = status_json.get("data", [])
    except Exception as e:
        logger.exception("Error calling current_status in /status")
        devices_info = []

    if not devices_info:
        html = f"""
        <!DOCTYPE html>
        <html lang="th">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>ยังไม่มีอุปกรณ์ที่ผูกกับห้องนี้</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f3f4f6;
                    color: #111827;
                    min-height: 100vh;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 16px;
                }}
                .card {{
                    background: #ffffff;
                    border-radius: 18px;
                    padding: 24px 20px;
                    box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                    max-width: 440px;
                    width: 100%;
                    border: 1px solid #e5e7eb;
                    text-align: center;
                }}
                h1 {{
                    font-size: 1.35rem;
                    margin-bottom: 8px;
                }}
                p {{
                    font-size: 0.95rem;
                    line-height: 1.5;
                    margin: 4px 0;
                }}
                .badge {{
                    display: inline-block;
                    padding: 3px 9px;
                    border-radius: 999px;
                    background: #fee2e2;
                    color: #b91c1c;
                    font-size: 0.78rem;
                    margin-bottom: 6px;
                }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="badge">No Devices</div>
                <h1>ยังไม่มีอุปกรณ์ที่ผูกกับห้องแชทนี้</h1>
                <p>กรุณาใช้คำสั่ง <b>/register</b> ในห้อง LINE นี้</p>
                <p>เพื่อผูก Device ID กับห้องแชท แล้วจึงกลับมาดูสถานะอีกครั้ง</p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    # สร้างการ์ดอุปกรณ์แต่ละตัว
    cards_html = ""
    for d in devices_info:
        did = str(d.get("id", "-"))
        unit = d.get("unit") or did
        status = (d.get("status") or "").lower()
        lastupdate_raw = d.get("lastupdate", "-")
        lastupdate = format_ts_th(lastupdate_raw) if lastupdate_raw not in (None, "-", "") else "-"

        temp = _safe_float(d.get("temp"), default=0.0)
        humid = _safe_float(d.get("humid"), default=0.0)
        hic = _safe_float(d.get("hic"), default=0.0)
        flag = d.get("flag", "")

        if status == "online":
            status_text = "ออนไลน์"
            status_class = "status-online"
            status_icon = "🟢"
        elif status == "offline":
            status_text = "ออฟไลน์"
            status_class = "status-offline"
            status_icon = "⚪️"
        else:
            status_text = status or "-"
            status_class = "status-unknown"
            status_icon = "⚪️"

        cards_html += f"""
        <div class="device-card">
            <div class="device-header">
                <div>
                    <div class="device-title">{unit}</div>
                    <div class="device-sub">Device ID: <b>{did}</b></div>
                </div>
                <div class="status-pill {status_class}">
                    <span>{status_icon}</span>
                    <span>{status_text}</span>
                </div>
            </div>
            <div class="device-body">
                <div class="metric">
                    <div class="metric-label">อุณหภูมิ</div>
                    <div class="metric-value">{temp:.1f}<span class="metric-unit">°C</span></div>
                </div>
                <div class="metric">
                    <div class="metric-label">ความชื้น</div>
                    <div class="metric-value">{humid:.1f}<span class="metric-unit">%RH</span></div>
                </div>
                <div class="metric">
                    <div class="metric-label">Heat Index</div>
                    <div class="metric-value">{hic:.1f}<span class="metric-unit">°C</span></div>
                </div>
            </div>
            <div class="device-footer">
                <div class="flag-pill">สถานะเซนเซอร์: <b>{flag}</b></div>
                <div class="lastupdate">อัปเดตล่าสุด: {lastupdate}</div>
            </div>
        </div>
        """

    # HTML หลัก
    html = f"""
    <!DOCTYPE html>
    <html lang="th">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>สถานะอุปกรณ์</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #f3f4f6;
                color: #111827;
                margin: 0;
                padding: 16px;
            }}
            .container {{
                max-width: 960px;
                margin: 0 auto;
            }}
            .card {{
                background: #ffffff;
                border-radius: 18px;
                padding: 18px 16px 20px;
                box-shadow: 0 10px 25px rgba(15,23,42,0.12);
                border: 1px solid #e5e7eb;
                margin-bottom: 16px;
            }}
            .header-title {{
                font-size: 1.35rem;
                margin: 0 0 4px 0;
            }}
            .header-sub {{
                font-size: 0.86rem;
                color: #6b7280;
            }}
            .header-sub span {{
                word-break: break-all;
            }}
            .device-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
                gap: 12px;
                margin-top: 8px;
            }}
            .device-card {{
                background: #ffffff;
                border-radius: 16px;
                border: 1px solid #e5e7eb;
                padding: 12px 12px 14px;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}
            .device-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 8px;
            }}
            .device-title {{
                font-size: 1rem;
                font-weight: 600;
            }}
            .device-sub {{
                font-size: 0.8rem;
                color: #6b7280;
            }}
            .status-pill {{
                display: inline-flex;
                align-items: center;
                gap: 4px;
                padding: 4px 10px;
                border-radius: 999px;
                font-size: 0.8rem;
                font-weight: 500;
            }}
            .status-online {{
                background: #dcfce7;
                color: #166534;
            }}
            .status-offline {{
                background: #fee2e2;
                color: #b91c1c;
            }}
            .status-unknown {{
                background: #e5e7eb;
                color: #374151;
            }}
            .device-body {{
                display: flex;
                justify-content: space-between;
                gap: 8px;
                margin-top: 4px;
            }}
            .metric {{
                flex: 1;
                background: #f9fafb;
                border-radius: 12px;
                padding: 6px 8px;
            }}
            .metric-label {{
                font-size: 0.76rem;
                color: #6b7280;
            }}
            .metric-value {{
                font-size: 1rem;
                font-weight: 600;
                margin-top: 2px;
            }}
            .metric-unit {{
                font-size: 0.75rem;
                margin-left: 2px;
                color: #6b7280;
            }}
            .device-footer {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 8px;
                margin-top: 4px;
                flex-wrap: wrap;
            }}
            .flag-pill {{
                background: #eff6ff;
                color: #1d4ed8;
                border-radius: 999px;
                padding: 4px 8px;
                font-size: 0.78rem;
            }}
            .lastupdate {{
                font-size: 0.78rem;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <div class="header-title">สถานะอุปกรณ์</div>
                <div class="header-sub">
                    LINE: <span>{line_id}</span><br />
                </div>
            </div>

            <div class="device-grid">
                {cards_html}
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
