from groq import Groq
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import os
import json
import threading
import subprocess
import datetime

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

MEMORY_FILE = "user_memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_memory(memory):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

user_memory = load_memory()

APPS = {
    "edge": "msedge", "chrome": "chrome", "notepad": "notepad",
    "calculator": "calc", "minecraft": "minecraft:",
    "valorant": r"C:\Riot Games\VALORANT\live\VALORANT.exe",
    "steam": "steam", "discord": "discord", "vscode": "code",
}

def open_program(program_name: str) -> str:
    key = program_name.lower().strip()
    if key in APPS:
        try:
            subprocess.Popen(['start', '', APPS[key]], shell=True)
            return f"✅ Opened {program_name}."
        except:
            return f"❌ Failed to open {program_name}."
    return f"Unknown: {program_name}"

def play_song(song_name: str) -> str:
    try:
        search_query = song_name.replace(" ", "+")
        os.system(f'start https://music.youtube.com/search?q={search_query}')
        return f"🎵 Playing '{song_name}'."
    except:
        return "Failed to play song."

def save_user_info(key: str, value: str, user_id: str) -> str:
    if user_id not in user_memory:
        user_memory[user_id] = {}
    user_memory[user_id][key.lower()] = value
    save_memory(user_memory)
    return f"✅ Saved: {key} = {value}"

tools = [
    {"type": "function", "function": {"name": "open_program", "description": "Open programs/games", "parameters": {"type": "object", "properties": {"program_name": {"type": "string"}}, "required": ["program_name"]}}},
    {"type": "function", "function": {"name": "play_song", "description": "Play song on YouTube Music", "parameters": {"type": "object", "properties": {"song_name": {"type": "string"}}, "required": ["song_name"]}}},
    {"type": "function", "function": {"name": "save_user_info", "description": "Save any personal information (name, age, favorite color, etc.)", "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}}}
]

available_functions = {
    "open_program": open_program,
    "play_song": play_song,
    "save_user_info": lambda key, value, user_id="terminal": save_user_info(key, value, user_id)
}

conversations = {}

def get_system_prompt(user_id):
    memory = user_memory.get(user_id, {})
    memory_text = "\n".join([f"{k}: {v}" for k, v in memory.items()])
    
    return f"""You are Jarvis, a highly intelligent personal assistant.

=== LONG TERM MEMORY ===
{memory_text or "No information saved yet."}

Rules:
- If the user tells you their name, age, or any personal info → immediately use the save_user_info tool.
- After saving, confirm it briefly.
- Always remember and use the saved information in future responses.
- Do not repeat actions like playing songs unless asked again."""

def ask_jarvis(conversation_id, user_input):
    if conversation_id not in conversations:
        conversations[conversation_id] = []

    messages = conversations[conversation_id]
    messages.append({"role": "user", "content": user_input})

    if len(messages) > 20:
        messages = messages[-18:]

    full_messages = [{"role": "system", "content": get_system_prompt(conversation_id)}] + messages

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=full_messages,
        tools=tools,
        temperature=0.65,
    )

    response_message = response.choices[0].message
    tool_calls = response_message.tool_calls

    if tool_calls:
        messages.append(response_message)
        for tool_call in tool_calls:
            func_name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)
            if func_name == "save_user_info":
                args["user_id"] = conversation_id
            result = available_functions[func_name](**args)
            messages.append({"tool_call_id": tool_call.id, "role": "tool", "name": func_name, "content": str(result)})

        # Final answer
        final_response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
        )
        final_text = final_response.choices[0].message.content
        messages.append({"role": "assistant", "content": final_text})
        return final_text
    else:
        messages.append({"role": "assistant", "content": response_message.content})
        return response_message.content


# ====================== Run (Telegram + Terminal) ======================
async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = f"telegram_{update.effective_user.id}"
    reply = ask_jarvis(user_id, update.message.text)
    await update.message.reply_text(reply)

def run_telegram_bot():
    app = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))
    app.run_polling()

def run_terminal():
    print("Jarvis is ready! (type 'exit' to quit)")
    while True:
        text = input("You: ")
        if text.lower() == "exit":
            break
        print("Jarvis:", ask_jarvis("terminal", text))

if __name__ == "__main__":
    threading.Thread(target=run_telegram_bot, daemon=True).start()
    run_terminal()