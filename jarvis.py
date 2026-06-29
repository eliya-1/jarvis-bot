from groq import Groq
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import os
import json
import threading

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
    return f"✅ Opened {program_name} (on host)."

def play_song(song_name: str) -> str:
    return f"🎵 Playing '{song_name}' on YouTube Music."

def save_user_info(key: str, value: str, user_id: str) -> str:
    if user_id not in user_memory:
        user_memory[user_id] = {}
    user_memory[user_id][key.lower()] = value
    save_memory(user_memory)
    return f"✅ Saved: {key} = {value}"

tools = [
    {"type": "function", "function": {"name": "open_program", "description": "Open programs", "parameters": {"type": "object", "properties": {"program_name": {"type": "string"}}, "required": ["program_name"]}}},
    {"type": "function", "function": {"name": "play_song", "description": "Play song", "parameters": {"type": "object", "properties": {"song_name": {"type": "string"}}, "required": ["song_name"]}}},
    {"type": "function", "function": {"name": "save_user_info", "description": "Save user info", "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}}}
]

available_functions = {
    "open_program": open_program,
   
