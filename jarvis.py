import os
import subprocess
import datetime
import logging
import requests
import webbrowser
from dotenv import load_dotenv
from groq import Groq
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

logging.getLogger("telegram").setLevel(logging.WARNING)

load_dotenv()
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

LOCAL_CITY = "Ateret"

# ====================== Tools ======================
def get_current_time():
    now = datetime.datetime.now()
    return f"🕒 השעה: {now.strftime('%H:%M')}"

def get_weather():
    try:
        r = requests.get(f"https://wttr.in/{LOCAL_CITY}?format=%C+%t", timeout=5)
        return f"🌤️ מזג אוויר ב{LOCAL_CITY}:\n{r.text.strip()}" if r.status_code == 200 else "❌ לא הצלחתי"
    except:
        return "❌ שגיאה"

def open_program(program_name: str):
    key = program_name.lower().strip()
    APPS = {"chrome": "chrome", "כרום": "chrome", "notepad": "notepad", "פנקס": "notepad", "calculator": "calc", "מחשבון": "calc"}
    if key in APPS:
        try:
            subprocess.Popen(['start', '', APPS[key]], shell=True)
            return f"✅ פתחתי {program_name}"
        except:
            return f"❌ לא הצלחתי"
    return f"❌ לא מכיר: {program_name}"

def open_phone_app(app_name: str):
    name = app_name.lower().strip()
    apps = {
        "וואטסאפ": "https://wa.me/",
        "whatsapp": "https://wa.me/",
        "יוטיוב": "https://youtube.com",
        "youtube": "https://youtube.com",
        "אינסטגרם": "https://instagram.com",
        "instagram": "https://instagram.com"
    }
    for k in apps:
        if k in name:
            webbrowser.open(apps[k])
            return f"📱 פתחתי {app_name}"
    return f"❌ לא מכיר: {app_name}"

# ====================== Handler ======================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.voice:
        await update.message.reply_text("🎙️ קיבלתי קול! תכתוב לי.")
        return

    reply = ask_jarvis(update.message.text)
    await update.message.reply_text(reply)

def ask_jarvis(user_input):
    text = user_input.lower().strip()

    if any(w in text for w in ["שעה", "time"]):
        return get_current_time()
    if any(w in text for w in ["מזג", "weather"]):
        return get_weather()

    if any(w in text for w in ["פתח", "open", "תפתח"]):
        name = user_input.replace("פתח", "").replace("open", "").replace("תפתח", "").strip()
        if any(word in name.lower() for word in ["יוטיוב", "youtube", "אינסטגרם", "instagram", "וואטסאפ", "whatsapp"]):
            return open_phone_app(name)
        return open_program(name)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": "You are Jarvis. Answer in Hebrew."}, {"role": "user", "content": user_input}],
            temperature=0.7
        )
        return response.choices[0].message.content
    except:
        return "שגיאה, תנסה שוב"

# ====================== Run ======================
if __name__ == "__main__":
    token = os.getenv("TELEGRAM_TOKEN")
    if token:
        app = Application.builder().token(token).build()
        app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))
        print("🤖 Jarvis is running...")
        app.run_polling()
    else:
        print("No TELEGRAM_TOKEN found")
