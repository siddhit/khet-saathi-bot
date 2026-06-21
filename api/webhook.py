import os
import re
import json
import requests
import anthropic
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]
ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

GRAPH_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

client = anthropic.Anthropic()

# Upstash Redis via REST API — HTTP calls, not TCP sockets.
# Survives across Vercel cold starts; every conversation persists for 24h.
KV_REST_API_URL = os.environ["KV_REST_API_URL"]
KV_REST_API_TOKEN = os.environ["KV_REST_API_TOKEN"]
MAX_HISTORY_TURNS = 10
HISTORY_EXPIRY = 86400  # 24 hours


def get_history(phone: str) -> list[dict]:
    """Fetch this sender's conversation history from Upstash."""
    resp = requests.get(
        f"{KV_REST_API_URL}/get/chat:{phone}",
        headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"},
        timeout=5,
    )
    result = resp.json().get("result")
    return json.loads(result) if result else []


def update_history(phone: str, role: str, content: str) -> None:
    """Append a turn and write back to Upstash with a 24h expiry."""
    history = get_history(phone)
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY_TURNS:
        history = history[-MAX_HISTORY_TURNS:]
    requests.post(
        f"{KV_REST_API_URL}/pipeline",
        headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}", "Content-Type": "application/json"},
        json=[["SET", f"chat:{phone}", json.dumps(history), "EX", str(HISTORY_EXPIRY)]],
        timeout=5,
    )


def validate_and_parse_response(text: str) -> dict | None:
    """Parse Claude's JSON response; strips markdown fences if present."""
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.MULTILINE)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    known_fields = {"diagnosis", "immediate_actions", "treatments", "follow_up_questions", "severity"}
    if not known_fields.intersection(parsed.keys()):
        return None
    return parsed


def format_for_whatsapp(advice: dict) -> str:
    """Convert structured JSON advice into WhatsApp-formatted text."""
    lines = []

    diagnosis = advice.get("diagnosis")
    if diagnosis:
        primary = diagnosis["primary_suspect"].replace("_", " ").title()
        lines.append(f"*Most likely:* {primary}")
        others = diagnosis.get("other_suspects", [])
        if others:
            lines.append(f"_Also check:_ {', '.join(o.replace('_', ' ') for o in others)}")

    immediate_actions = advice.get("immediate_actions", [])
    if immediate_actions:
        lines.append("\n*Do this today:*")
        for action in immediate_actions:
            lines.append(f"• {action}")

    treatments = advice.get("treatments", [])
    if treatments:
        lines.append("\n*If needed:*")
        for t in treatments:
            condition = t["condition"].replace("_", " ").title()
            lines.append(f"• *{condition}:* {t.get('product', '')} — {t.get('dosage', '')}")
            if t.get("repeat"):
                lines.append(f"  Repeat: {t['repeat']}")

    questions = advice.get("follow_up_questions", [])
    if questions:
        lines.append(f"\n{questions[0]}")

    return "\n".join(lines)


def send_whatsapp_message(to: str, body: str) -> None:
    """POST a text message to a recipient via the WhatsApp Graph API."""
    requests.post(
        GRAPH_API_URL,
        json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}},
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
        timeout=10,
    ).raise_for_status()


def extract_text_message(body: dict) -> tuple[str, str] | None:
    """Return (sender_phone, message_text) or None if not a text message."""
    try:
        change = body["entry"][0]["changes"][0]["value"]
        if "messages" not in change:
            return None
        message = change["messages"][0]
        if message["type"] != "text":
            return None
        return message["from"], message["text"]["body"]
    except (KeyError, IndexError):
        return None


def detect_language(text: str) -> str:
    """Identify the primary script in the message using Unicode block counts."""
    scores = {
        "Gujarati": sum(1 for c in text if "઀" <= c <= "૿"),
        "Hindi": sum(1 for c in text if "ऀ" <= c <= "ॿ"),
        "English": sum(1 for c in text if c.isascii() and c.isalpha()),
    }
    return max(scores, key=scores.get)


VALID_CATEGORIES = {"crop_disease", "farm_strategy", "off_topic", "greeting"}


def classify_message(text: str) -> str:
    """Classify incoming message into one of four routing categories."""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system=(
                "You are a message classifier for a farm advisory bot that only handles on-farm crop management. "
                "Classify the message into exactly one of these four categories:\n"
                "- crop_disease: plant health, pests, diseases, or visible crop damage\n"
                "- farm_strategy: on-farm decisions such as irrigation, fertiliser, planting, harvesting, or crop rotation\n"
                "- off_topic: market prices, weather forecasts, general knowledge, or anything not about on-farm crop management\n"
                "- greeting: a greeting or introduction with no specific question\n"
                "Return only the category name, nothing else."
            ),
            messages=[{"role": "user", "content": text}],
        )
        # Normalize hyphens/spaces to underscores and strip stray punctuation
        # before checking against valid categories (model may use "off-topic" etc.)
        raw = response.content[0].text.strip().lower()
        category = re.sub(r"[-\s]+", "_", raw).strip("_.,!?;:")
        return category if category in VALID_CATEGORIES else "crop_disease"
    except Exception:
        return "crop_disease"


SYSTEM_PROMPT = """You are an expert agronomist specialising in Saurashtra crops — onion, cotton, and groundnut. You have advised farmers in the Mota Asrana area near Mahuva, Gujarat for 20 years. You give practical, actionable advice tailored to each farmer's situation.

You are advising farm supervisors at a farm in Mota Asrana, near Mahuva, Gujarat, India on plant condition or seasonal strategy.

Rules:
- Use simple, everyday words — no technical jargon.
- Answer only what was asked — do not mix plant-level and strategy-level advice.
- No greetings, sign-offs, or preambles.
- Always respond with valid JSON only. No prose before or after. No markdown code blocks.
- JSON keys and enum-style slug values (primary_suspect, other_suspects, condition) must always be in English.
- All human-readable text values (immediate_actions, follow_up_questions, product, dosage, method, timing, repeat) must be written in {language}.
- If the message is a greeting or not an agricultural question, respond with ONLY a follow_up_questions field in JSON asking what problem they need help with. No diagnosis, no actions.

Respond with this JSON structure:
{
  "diagnosis": {"primary_suspect": "slug", "other_suspects": ["slug"]},
  "immediate_actions": ["..."],
  "treatments": [{"condition": "slug", "product": "...", "dosage": "...", "method": "...", "timing": "...", "repeat": "..."}],
  "follow_up_questions": ["..."],
  "severity": "low|medium|high"
}"""


FARM_STRATEGY_PROMPT = """You are a farm advisor specialising in seasonal and operational decisions for Saurashtra farms — onion, cotton, and groundnut near Mahuva and Mota Asrana, Gujarat.

You cover planting and harvest timing, irrigation scheduling, fertiliser timing, crop rotation, and storage decisions.

You have no crop disease knowledge — if the farmer raises a disease or pest problem, direct them to ask about it separately.

Rules:
- Respond conversationally in plain text in {language}.
- Use simple, everyday words — no technical jargon.
- No JSON, no schema, no bullet lists unless naturally helpful.
- No greetings, sign-offs, or preambles."""


def handle_crop_disease(sender: str, text: str, language: str) -> None:
    try:
        history = get_history(sender)
        print(f"[agent] routing crop_disease from {sender}")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT.replace("{language}", language),
            messages=history + [{"role": "user", "content": text}],
        )
        reply = response.content[0].text
        update_history(sender, "user", text)
        update_history(sender, "assistant", reply)
        parsed = validate_and_parse_response(reply)
        if parsed:
            send_whatsapp_message(sender, format_for_whatsapp(parsed))
        else:
            send_whatsapp_message(sender, "Sorry, something went wrong. Please try again.")
    except Exception:
        pass  # Never let an exception prevent the 200 to Meta


def handle_farm_strategy(sender: str, text: str, language: str) -> None:
    try:
        history = get_history(sender)
        print(f"[agent] routing farm_strategy from {sender}")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=FARM_STRATEGY_PROMPT.replace("{language}", language),
            messages=history + [{"role": "user", "content": text}],
        )
        reply = response.content[0].text
        update_history(sender, "user", text)
        update_history(sender, "assistant", reply)
        send_whatsapp_message(sender, reply)
    except Exception:
        pass  # Never let an exception prevent the 200 to Meta


class handler(BaseHTTPRequestHandler):
    """Vercel Python runtime expects a class named `handler` extending BaseHTTPRequestHandler."""

    def do_GET(self):
        """Webhook verification handshake — Meta calls this once on registration."""
        params = parse_qs(urlparse(self.path).query)
        mode = params.get("hub.mode", [None])[0]
        token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if mode == "subscribe" and token == VERIFY_TOKEN:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode())
        else:
            self.send_response(403)
            self.end_headers()

    def do_POST(self):
        """Receive a WhatsApp message, call Claude, reply — then return 200."""
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            self._ok()
            return

        result = extract_text_message(body)
        if result is None:
            self._ok()
            return  # Status update or non-text — ignore

        sender, text = result
        classification = classify_message(text)
        print(f"[classify] {classification}")
        if classification == "off_topic":
            try:
                send_whatsapp_message(sender, "I can only help with farming questions about onion, cotton, and groundnut. What crop problem can I help you with?")
            except Exception:
                pass
            self._ok()
            return
        if classification == "greeting":
            try:
                send_whatsapp_message(sender, "Hello! I'm your farm advisor for the Mota Asrana farm. What crop or field problem can I help you with today?")
            except Exception:
                pass
            self._ok()
            return
        language = detect_language(text)

        if classification == "crop_disease":
            handle_crop_disease(sender, text, language)
        elif classification == "farm_strategy":
            handle_farm_strategy(sender, text, language)

        self._ok()

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, format, *args):
        pass  # Suppress default stderr logging
