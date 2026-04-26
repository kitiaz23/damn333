import os
import logging
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=["*"])

GROQ_KEY     = os.environ.get("GROQ_KEY")
GOOGLE_KEY   = os.environ.get("GOOGLE_KEY")
CEREBRAS_KEY = os.environ.get("CEREBRAS_KEY")

# Log key status saat startup (berjalan di gunicorn juga)
log.info("=== ArbitBot API Starting ===")
log.info(f"GOOGLE_KEY   : {'SET ✅' if GOOGLE_KEY   else 'MISSING ⚠️'}")
log.info(f"CEREBRAS_KEY : {'SET ✅' if CEREBRAS_KEY else 'MISSING ⚠️'}")
log.info(f"GROQ_KEY     : {'SET ✅' if GROQ_KEY     else 'MISSING ⚠️'}")

SYSTEM_PROMPT = """
You are ArbitBot, official Customer Service of ArbitFinder (arbitfinder.my.id).
CRITICAL: Always reply in the SAME language the user writes in.
Keep answers SHORT — max 3-4 sentences. No long paragraphs.

ABOUT ARBITFINDER:
- Crypto arbitrage tools platform since 2022
- Supports 9 exchanges: Binance, HTX, OKX, Gate.io, KuCoin, MEXC, CoinEx, Indodax, Tokocrypto
- Signals refresh every 30 seconds, up to 500+ signals
- Features: linear arbitrage (spot cross-exchange), funding rate arbitrage, live dual orderbook
- Free guidebook: https://arbitfinder.my.id/FAQ/What_Is_Arbitrage

MEMBERSHIP:
- FREE: 20 signals (free)
- BASIC 1 Month: USD 15 / IDR 200,000
- STANDARD 3 Months: USD 29 / IDR 450,000
- PREMIUM+ 1 Year: USD 69 / IDR 1,100,000
- Payment: Crypto, QRIS, Midtrans

HOW TO JOIN: visit arbitfinder.my.id → Register → fill data → choose plan → pay

COMMUNITY: https://t.me/ArbitFinderGroup

MEMBERSHIP ISSUES (paid but not premium yet): contact @ArbitFinderAdmin on Telegram

TECHNICAL KNOWLEDGE (answer briefly):
- Arbitrage: buy coin cheap on exchange A, sell higher on exchange B
- Orderbook: list of pending buy (bid) and sell (ask) orders
- Liquidity: available order volume. High = safe, Low = slippage risk
- Funding rate: periodic payment between long & short traders in futures
- On-chain withdraw: choose correct network — wrong network = assets lost

RULES: Only answer about ArbitFinder & crypto arbitrage.
If out of scope: "Sorry, I can only help with ArbitFinder and crypto arbitrage. Visit arbitfinder.my.id"
DO NOT: predict prices, give investment advice, answer non-crypto topics.
"""

RATE_LIMIT_MSG = (
    "Our assistant is very busy right now 😊\n\n"
    "For further questions, join our community where members and admins are ready to help:\n\n"
    "👥 ArbitFinder Community Group\n"
    "🔗 https://t.me/ArbitFinderGroup\n\n"
    "Or contact our official admin: @ArbitFinderAdmin"
)

def call_ai(messages: list) -> str:
    # ── Provider 1: Google AI Studio ─────────────────────────────────────────
    if GOOGLE_KEY:
        try:
            google_msgs = []
            sys_txt = ""
            for m in messages:
                if   m["role"] == "system":    sys_txt = m["content"]
                elif m["role"] == "user":      google_msgs.append({"role":"user",  "parts":[{"text":m["content"]}]})
                elif m["role"] == "assistant": google_msgs.append({"role":"model", "parts":[{"text":m["content"]}]})

            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GOOGLE_KEY}",
                json={
                    "system_instruction": {"parts":[{"text":sys_txt}]},
                    "contents": google_msgs,
                    "generationConfig": {"maxOutputTokens":250,"temperature":0.4}
                },
                timeout=15
            )
            if resp.status_code == 200:
                log.info("AI: Google OK")
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            else:
                log.warning(f"Google failed: HTTP {resp.status_code} — {resp.text[:100]}")
        except Exception as e:
            log.warning(f"Google exception: {e}")

    # ── Provider 2: Cerebras ──────────────────────────────────────────────────
    if CEREBRAS_KEY:
        try:
            resp = requests.post(
                "https://api.cerebras.ai/v1/chat/completions",
                headers={"Authorization":f"Bearer {CEREBRAS_KEY}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b","messages":messages,"max_tokens":250,"temperature":0.4},
                timeout=15
            )
            if resp.status_code == 200:
                log.info("AI: Cerebras OK")
                return resp.json()["choices"][0]["message"]["content"].strip()
            else:
                log.warning(f"Cerebras failed: HTTP {resp.status_code} — {resp.text[:100]}")
        except Exception as e:
            log.warning(f"Cerebras exception: {e}")

    # ── Provider 3: Groq ──────────────────────────────────────────────────────
    if GROQ_KEY:
        try:
            client = Groq(api_key=GROQ_KEY)
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                max_tokens=250,
                temperature=0.4
            )
            log.info("AI: Groq OK")
            return resp.choices[0].message.content.strip()
        except Exception as e:
            log.error(f"Groq exception: {e}")

    # ── Semua gagal ───────────────────────────────────────────────────────────
    log.error("ALL AI providers failed — returning community message")
    return RATE_LIMIT_MSG

user_histories = {}

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data     = request.json or {}
        user_id  = data.get("user_id", "web_anon")
        user_msg = data.get("message", "").strip()

        log.info(f"[REQ] user={user_id} | msg={user_msg[:60]}")

        if not user_msg:
            return jsonify({"reply": "Silakan ketik pertanyaan kamu 😊"})

        if user_id not in user_histories:
            user_histories[user_id] = []

        history = user_histories[user_id]
        history.append({"role":"user","content":user_msg})

        messages = [{"role":"system","content":SYSTEM_PROMPT}] + history[-10:]
        reply    = call_ai(messages)

        history.append({"role":"assistant","content":reply})
        if len(history) > 20:
            user_histories[user_id] = history[-20:]

        log.info(f"[REP] {reply[:80]}")
        return jsonify({"reply": reply})

    except Exception as e:
        log.error(f"[ERR] chat: {e}")
        return jsonify({"reply": "Terjadi error sementara. Silakan coba lagi."}), 500

@app.route("/health", methods=["GET"])
def health():
    keys = {
        "google":   bool(GOOGLE_KEY),
        "cerebras": bool(CEREBRAS_KEY),
        "groq":     bool(GROQ_KEY),
    }
    return jsonify({"status":"ok","service":"ArbitBot API","keys":keys})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
