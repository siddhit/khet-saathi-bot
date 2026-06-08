import os
import json
import hmac
import hashlib
import requests
import anthropic
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]
ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]

GRAPH_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

client = anthropic.Anthropic()

def send_whatsapp_message(to: str, body: str) -> None:
    """Fire-and-forget: POST a text message to a recipient via the Graph API."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    response = requests.post(GRAPH_API_URL, json=payload, headers=headers, timeout=10)
    response.raise_for_status()


def extract_text_message(body: dict) -> tuple[str, str] | None:
    """
    Pull (sender_phone, message_text) from the webhook payload.
    Returns None if this isn't a text message (image, audio, status update, etc.).
    """
    try:
        entry = body["entry"][0]
        change = entry["changes"][0]["value"]

        # Status updates (read receipts, delivered confirmations) arrive on the
        # same endpoint — they have no "messages" key, only "statuses".
        if "messages" not in change:
            return None

        message = change["messages"][0]
        if message["type"] != "text":
            return None

        sender = message["from"]
        text = message["text"]["body"]
        return sender, text

    except (KeyError, IndexError):
        return None


class handler(BaseHTTPRequestHandler):
    """
    Vercel's Python runtime expects a class named `handler` that extends
    BaseHTTPRequestHandler. Each HTTP method maps to a do_METHOD function.
    """

    def do_GET(self):
        """
        Webhook verification handshake.
        Meta calls this once when you register the webhook URL in the console.
        """
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        mode = params.get("hub.mode", [None])[0]
        token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if mode == "subscribe" and token == VERIFY_TOKEN:
            # Echo the challenge back — this is how Meta knows you control this URL
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode())
        else:
            self.send_response(403)
            self.end_headers()

    def do_POST(self):
        """
        Incoming message handler.

        Phase 0 pattern (synchronous):
        - Parse payload
        - Call WhatsApp API to send reply
        - Return 200

        Phase 1+ will change this to:
        - Parse payload, enqueue job
        - Return 200 immediately
        - Worker sends reply asynchronously
        (Required once AI processing time exceeds ~3–5 seconds)
        """
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # Always return 200 first — if we return an error, Meta will retry
        # repeatedly. A 200 tells Meta "received, handled."
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

        # Parse and process after acknowledging
        # Note: in Vercel serverless, code after writing the response still runs
        # within the same invocation — this is fine for Phase 0.
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return

        result = extract_text_message(body)
        if result is None:
            return  # Not a text message — ignore for now

        sender, text = result
        # Messae to send to Claude
        prompt = '''
         You are an expert agronomist expert in Saurashtra crops, especially onion, cotton and groundnut. You have been helping farmers in the region for 20 years, providing advice on crop management, pest control, and sustainable farming practices. Your expertise has led to increased yields and improved livelihoods for many farmers in Saurashtra. You are known for your practical and actionable advice, tailored to the specific needs of each farmer. You are passionate about promoting sustainable agriculture and empowering farmers with knowledge and resources to succeed.

         You are advising farmers who are supervisors of a particular farm located in Mota Asrana, near Mahuva, Gujarat, India  on the condition of their plant or on overall strategy for various operations of a particular season.      
                          
        Respond in a way that   
        is in simple words as   
        this will be translated 
        to Gujarati. A          
        sophisticated,          
        terminology-heavy       
        message will not work   
        with farmers. Mixing    
        high-level strategy     
        with a per plant        
        question or vice versa  
        will also confuse them.  No     
        intro or outro or       
        preambles either.
         '''
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=prompt,
            messages=[{"role": "user", "content": text}]
        )
        reply = response.content[0].text 
        send_whatsapp_message(sender, reply)

    def log_message(self, format, *args):
        pass  # Suppress BaseHTTPRequestHandler's default stderr logging
