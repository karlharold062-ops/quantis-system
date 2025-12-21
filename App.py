import os
import requests
from flask import Flask

app = Flask(__name__)

# Récupération du lien Discord depuis Render
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def send_to_discord(message):
    if DISCORD_WEBHOOK_URL:
        data = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=data)

@app.route('/')
def home():
    send_to_discord("✅ Le Système Quantique est en ligne sur Discord !")
    return "Bot en cours d'exécution..."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
