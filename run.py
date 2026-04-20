import os
import sys
import json
import requests
import re
import subprocess
import shutil
import time
import logging
import concurrent.futures
import base64
import sqlite3
import difflib
import threading
from urllib.parse import urlparse

# --- External Dependencies ---
from bs4 import BeautifulSoup

# Mematikan warning
logging.captureWarnings(True)

# --- UI Modules ---
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.text import Text
from rich.table import Table
from rich.rule import Rule
from rich.align import Align
from rich.syntax import Syntax
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

# --- Input Modules (Prompt Toolkit Advanced) ---
from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.shortcuts import confirm, yes_no_dialog, input_dialog, checkboxlist_dialog
from prompt_toolkit.styles import Style as PromptStyle
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import NestedCompleter, PathCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.validation import Validator, ValidationError
from prompt_toolkit.lexers import PygmentsLexer
from pygments.lexers.markup import MarkdownLexer

# --- CONFIGURATION & STORAGE ---
PICA_DIR = os.path.expanduser("~/.pica_agent")
os.makedirs(PICA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(PICA_DIR, "config.json")
SESSION_DIR = os.path.join(PICA_DIR, "sessions")
HISTORY_FILE = os.path.join(PICA_DIR, "history.txt")
VAULT_FILE = os.path.join(PICA_DIR, "vault.json")

BACKUP_DIR = os.path.join(PICA_DIR, "backups")
LOGS_DIR = os.path.join(PICA_DIR, "logs")
MISTAKES_FILE = os.path.join(PICA_DIR, "mistakes.json")

os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Skema Warna Neo-Brutalism
COLOR_PRIMARY = "gold3"
COLOR_ACCENT = "bold yellow"
COLOR_USER = "bold bright_cyan"
COLOR_SYSTEM = "magenta"
COLOR_ERROR = "bold red1"
COLOR_SUCCESS = "bold green3"

console = Console()

# --- CUSTOM STYLE UNTUK POP-UP DIALOG ---
PICA_DIALOG_STYLE = PromptStyle.from_dict({
    'dialog':             'bg:#222222 fg:#ffffff',       
    'dialog frame.label': 'bg:#222222 fg:#ffcc00 bold',  
    'dialog.body':        'bg:#111111 fg:#ffffff',       
    'text-area':          'bg:#000000 fg:#00ffff',       
    'button':             'bg:#333333 fg:#ffffff',       
    'button.focused':     'bg:#ffcc00 fg:#000000 bold',  
    'checkbox':           'bg:#111111 fg:#ffffff',       
    'checkbox-checked':   'fg:#00ff00 bold',             
    'checkbox-selected':  'bg:#333333',                  
})

# --- STATE MANAGEMENT (CLI GLOBALS) ---
config = {
    "token": "",
    "temperature": 0.7,
    "current_session": "default",
    "safe_mode": False,
    "vi_mode": False,
    "macros": {},
    "telegram_bots": [] # Format: [{"token": "...", "admin": "..."}]
}

chat_history = []
is_build_mode = False
daemons = {} 
token_estimate = 0
project_todos = []

def load_json(filepath, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f: return json.load(f)
        except: pass
    return default

def save_json(filepath, data):
    with open(filepath, "w") as f: json.dump(data, f, indent=4)

config.update(load_json(CONFIG_FILE, config))
vault = load_json(VAULT_FILE, {})

def save_session_state():
    state = {
        "chat_history": chat_history,
        "project_todos": project_todos,
        "is_build_mode": is_build_mode,
        "token_estimate": token_estimate
    }
    filepath = os.path.join(SESSION_DIR, f"{config['current_session']}.json")
    save_json(filepath, state)

def load_session_state():
    global chat_history, project_todos, is_build_mode, token_estimate
    filepath = os.path.join(SESSION_DIR, f"{config['current_session']}.json")
    state = load_json(filepath, {})
    if state:
        chat_history = state.get("chat_history", [])
        project_todos = state.get("project_todos", [])
        is_build_mode = state.get("is_build_mode", False)
        token_estimate = state.get("token_estimate", 0)

load_session_state()

def decode_token(encoded_token):
    try:
        if encoded_token.startswith("pica-"):
            return bytes.fromhex(encoded_token.replace("pica-", "")).decode('utf-8')
    except: pass
    return None

def calculate_tokens(text, tg_state=None):
    if tg_state is not None:
        tg_state["token_estimate"] += len(str(text)) // 4
    else:
        global token_estimate
        token_estimate += len(str(text)) // 4

# --- CORE UTILS ---
def backup_file(path):
    if os.path.exists(path):
        try:
            bname = os.path.basename(path)
            ts = int(time.time())
            bpath = os.path.join(BACKUP_DIR, f"{bname}_{ts}.bak")
            shutil.copy2(path, bpath)
        except: pass

def log_mistake(error_msg):
    mistakes = load_json(MISTAKES_FILE, [])
    mistakes.append({"time": time.strftime("%Y-%m-%d %H:%M"), "error": error_msg[:300]})
    save_json(MISTAKES_FILE, mistakes[-5:])

def generate_tree(startpath, max_depth=2):
    tree_str = ""
    startpath = os.path.abspath(startpath)
    for root, dirs, files in os.walk(startpath):
        if "node_modules" in root or ".git" in root: continue
        level = root.replace(startpath, '').count(os.sep)
        if level > max_depth: continue
        indent = ' ' * 4 * level
        tree_str += f"{indent}📁 {os.path.basename(root)}/\n"
        subindent = ' ' * 4 * (level + 1)
        for f in files[:10]: tree_str += f"{subindent}📄 {f}\n"
        if len(files) > 10: tree_str += f"{subindent}... and {len(files)-10} more files\n"
    return tree_str if tree_str else "Directory is empty or invalid."

def generate_image(prompt, save_path):
    token = decode_token(config["token"])
    url = "https://raphael.app/api/generate-image"
    payload = {"prompt": prompt, "aspect": "1:1", "isSafeContent": False, "autoTranslate": True, "model_id": "raphael-basic", "number_of_images": 1, "highQuality": True, "fastMode": True}
    headers = {'User-Agent': "Mozilla/5.0", 'Content-Type': "application/json", 'referer': "https://raphael.app/"}
    proxies = {"http": f"http://{token}@p.webshare.io:80", "https": f"http://{token}@p.webshare.io:80"} if token else None
    try:
        res = requests.post(url, json=payload, headers=headers, proxies=proxies, verify=False).json()
        img_url = "https://raphael.app" + res["url"].replace("?wm=1", "")
        img_data = requests.get(img_url).content
        full_path = os.path.abspath(save_path)
        with open(full_path, 'wb') as f: f.write(img_data)
        return f"Image successfully generated and saved to {full_path}"
    except Exception as e: return f"Failed to generate image: {str(e)}"

def perform_web_search(query):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.post("https://lite.duckduckgo.com/lite/", data={"q": query}, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        results_str = ""
        count = 0
        for td in soup.find_all("td", class_="result-snippet"):
            if count >= 3: break
            snippet = td.text.strip()
            title_tr = td.parent.find_previous_sibling("tr")
            if title_tr:
                a_tag = title_tr.find("a")
                if a_tag:
                    title = a_tag.text.strip()
                    link = a_tag.get("href", "")
                    if "uddg=" in link:
                        try: link = requests.utils.unquote(link.split("uddg=")[1].split("&")[0])
                        except: pass
                    results_str += f"Title: {title}\nLink: {link}\nSnippet: {snippet}\n\n"
                    count += 1
        return results_str if results_str else "No results found."
    except Exception as e: return f"Search error: {str(e)}"

def capture_screenshot(target_url, device="desktop", save_path="screenshot.png"):
    try:
        url_api = "https://www.screenshotmachine.com/capture.php"
        payload = {'url': target_url, 'device': device, 'full': "on", 'cacheLimit': 0}
        headers = {'host': 'www.screenshotmachine.com', 'User-Agent': "Mozilla/5.0", 'referer': "https://www.screenshotmachine.com/"}
        res_post = requests.post(url_api, data=payload, headers=headers, timeout=30)
        set_cookie = res_post.headers.get('Set-Cookie', '')
        match = re.search(r'PHPSESSID=([^;]+)', set_cookie)
        if match:
            phpsessid = match.group(1)
            url_serve = "https://www.screenshotmachine.com/serve.php"
            params = {'file': "result"}
            headers_get = headers.copy()
            headers_get.update({'Accept': "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", 'Cookie': f"PHPSESSID={phpsessid}"})
            res_get = requests.get(url_serve, params=params, headers=headers_get, timeout=30)
            if 'image' in res_get.headers.get('Content-Type', ''):
                full_path = os.path.abspath(save_path)
                with open(full_path, 'wb') as f: f.write(res_get.content)
                return f"Successfully captured full-page screenshot of {target_url}. Saved to {full_path}"
            else: return f"Failed to capture screenshot."
        else: return "Failed to establish session with screenshot API."
    except Exception as e: return f"Error taking screenshot: {str(e)}"

def get_system_stats():
    stats = {"cpu_cores": os.cpu_count(), "ram_total": "Unknown", "ram_free": "Unknown", "disk_total": "Unknown", "disk_free": "Unknown"}
    try:
        disk = shutil.disk_usage('/')
        stats["disk_total"] = f"{disk.total / (1024**3):.2f} GB"
        stats["disk_free"] = f"{disk.free / (1024**3):.2f} GB"
    except: pass
    return f"CPU Cores: {stats['cpu_cores']}\nDisk Total: {stats['disk_total']} | Free: {stats['disk_free']}"

def fold_code(text_content, max_lines=15):
    lines = text_content.split('\n')
    if len(lines) > max_lines:
        first_part = "\n".join(lines[:5])
        last_part = "\n".join(lines[-5:])
        hidden_count = len(lines) - 10
        separator = f"\n\n# ... [ ✂️ {hidden_count} BARIS KODE DISEMBUNYIKAN ] ...\n\n"
        return first_part + separator + last_part
    return text_content

def get_sys_info():
    cwd = os.getcwd()
    os_name = sys.platform
    return f"OS: {os_name} | CWD: {cwd}"

# --- SYSTEM PROMPT CLI ---
def get_system_prompt_cli():
    mode_str = "BUILD MODE (Finish all Todo List items before <done></done>)" if is_build_mode else "CHAT MODE"
    todo_str = "Todo List is currently empty."
    if project_todos: todo_str = "\n".join([f"[{'x' if t['done'] else ' '}] {t['task']}" for t in project_todos])
    mistakes = load_json(MISTAKES_FILE, [])
    mistake_str = "No recent mistakes recorded." if not mistakes else "\n".join([f"- {m['time']}: {m['error']}" for m in mistakes])

    return f"""You are PICA AI, an autonomous developer agent. DO NOT BE LAZY.
CURRENT SYSTEM CONTEXT: {get_sys_info()}

[CRITICAL AGENT STATE]
CURRENT MODE: {mode_str}
CURRENT TODO LIST:
{todo_str}
[YOUR RECENT MISTAKES]:
{mistake_str}

XML TOOLS (1 tool per response):
1.  <cmd>command</cmd> : Run shell command.
2.  <write path="file">full code</write> : Overwrite/create file.
3.  <replace path="file.py"><old>code</old><new>code</new></replace> : Smart replace.
4.  <restore path="file.py"></restore> : Restore backup.
5.  <refactor path="src" ext=".js" old="v" new="l"></refactor> : Mass replace.
6.  <daemon>command</daemon> : Background server.
7.  <read_log pid="1234" lines="20"></read_log> : Read daemon log.
8.  <http method="POST" url="..."></http> : API Tester.
9.  <search>query</search> : Web search.
10. <index_dir keyword="text" path="."></index_dir> : Search keyword.
11. <tree path="." depth="2"></tree> : View directory tree.
12. <sql path="db">query</sql> : SQL queries.
13. <zip path="folder">name</zip> | <unzip path="file.zip">dest</unzip>
14. <image_gen path="img.png">prompt</image_gen> : AI image.
15. <screenshot url="..." device="desktop" path="web.png"></screenshot>
16. <quest type="yesno/choice/input" options="A,B">Question?</quest> : Ask user.
17. <todo action="create/checklist/list">Task</todo> : Manage tasks.

WORKFLOW RULES:
1. Use <build></build> and IMMEDIATELY <todo action="create"> to map out steps.
2. ALWAYS use <todo action="checklist"> to mark step done BEFORE starting next step.
3. Output <done></done> ONLY when ALL tasks are [x].
"""

# --- SYSTEM PROMPT TELEGRAM ---
def get_system_prompt_telegram(tg_state):
    b_mode = tg_state["is_build_mode"]
    todos = tg_state["project_todos"]

    mode_str = "BUILD MODE" if b_mode else "CHAT MODE"
    todo_str = "Todo List empty."
    if todos: todo_str = "\n".join([f"[{'x' if t['done'] else ' '}] {t['task']}" for t in todos])
    mistakes = load_json(MISTAKES_FILE, [])
    mistake_str = "None" if not mistakes else "\n".join([f"- {m['error']}" for m in mistakes])

    return f"""You are PICA AI, an autonomous developer agent.
You are communicating with the user via TELEGRAM BOT UI.

CURRENT SYSTEM CONTEXT: {get_sys_info()}

[TELEGRAM AGENT STATE]
CURRENT MODE: {mode_str}
CURRENT TODO LIST:
{todo_str}
MISTAKES AVOID: {mistake_str}

CRITICAL TELEGRAM RULES (NO EXCEPTIONS):
1. DO NOT output long raw source code in the conversational chat. The user is on mobile.
2. Write code SILENTLY using <write> and <replace> tags. Keep the conversational text (outside XML) short, friendly, and use emojis to report what you are doing.
3. When you need user input/choices, MUST use <quest> tags. They will automatically render as Native Telegram Buttons!
4. When a project is requested, use <zip path="folder_name">project_name</zip> just BEFORE <done></done>. The Telegram bot will automatically send the ZIP file document to the user!
5. Use <image_gen> and <screenshot> freely. The resulting images will be sent directly to the Telegram chat.

XML TOOLS (1 tool per response):
1.  <cmd>command</cmd> 
2.  <write path="file">code</write> 
3.  <replace path="file.py"><old>code</old><new>code</new></replace>
4.  <daemon>command</daemon>
5.  <read_log pid="1234" lines="20"></read_log>
6.  <search>query</search>
7.  <tree path="." depth="2"></tree>
8.  <zip path="folder">name</zip>
9.  <image_gen path="img.png">prompt</image_gen>
10. <screenshot url="..." device="desktop" path="web.png"></screenshot>
11. <quest type="yesno/choice/input" options="A,B">Question?</quest>
12. <todo action="create/checklist/list">Task</todo>

WORKFLOW:
1. Use <build></build> and <todo action="create"> to map out steps.
2. Check tasks with <todo action="checklist">.
3. End with <done></done>.
"""

# --- API INTEGRATION ---
def call_pica_api(messages, skip_summary=False, tg_state=None):
    token = decode_token(config["token"])
    if not token: return "ERROR_TOKEN"

    calculate_tokens(messages, tg_state)
    current_token_est = tg_state["token_estimate"] if tg_state else token_estimate

    if current_token_est > 30000 and not skip_summary:
        if not tg_state: console.print(f"[{COLOR_SYSTEM}]🔄 Auto-Summarizer bekerja...[/{COLOR_SYSTEM}]")
        summarize_history(tg_state)

    sys_prompt = get_system_prompt_telegram(tg_state) if tg_state else get_system_prompt_cli()
    
    url = "https://api.deepinfra.com/v1/openai/chat/completions"
    full_messages = [{"role": "system", "content": sys_prompt}] + messages
    payload = {"model": "Qwen/Qwen3.5-397B-A17B", "messages": full_messages, "stream": False, "temperature": config["temperature"]}
    proxies = {"http": f"http://{token}@p.webshare.io:80", "https": f"http://{token}@p.webshare.io:80"}

    try:
        res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, proxies=proxies, verify=False, timeout=120)
        res.raise_for_status()
        content = res.json()['choices'][0]['message']['content']
        calculate_tokens(content, tg_state)
        return content
    except Exception as e: return f"ERROR_API: {str(e)}"

def sub_agent_task(task_prompt):
    messages = [{"role": "user", "content": f"SUB-AGENT TASK: {task_prompt}. Return only the result."}]
    return call_pica_api(messages, skip_summary=True)

def summarize_history(tg_state=None):
    global chat_history, token_estimate 
    history = tg_state["chat_history"] if tg_state else chat_history
    if len(history) < 3: return
    sys_msg = [{"role": "system", "content": "Summarize the key context, code written, and project state from this conversation. Keep it concise."}]
    summary = call_pica_api(sys_msg + history, skip_summary=True, tg_state=tg_state)
    
    if tg_state:
        tg_state["chat_history"] = [{"role": "assistant", "content": f"[MEMORY SUMMARY]\n{summary}"}]
        tg_state["token_estimate"] = len(summary) // 4
    else:
        chat_history = [{"role": "assistant", "content": f"[MEMORY SUMMARY]\n{summary}"}]
        token_estimate = len(summary) // 4
        save_session_state()

# --- SMART REGEX PARSER & EXECUTOR ---
def parse_and_execute(text, session=None, is_telegram=False, tg_state=None):
    global daemons, vault, is_build_mode, project_todos
    
    b_mode = tg_state["is_build_mode"] if tg_state else is_build_mode
    todos = tg_state["project_todos"] if tg_state else project_todos
    
    extracted_files = [] 
    
    if "<build></build>" in text:
        b_mode = True
        text = text.replace("<build></build>", "").strip()
        if not is_telegram: console.print(Rule("🛠️ [bold yellow]MEMASUKI MODE BUILDER[/bold yellow] 🛠️", style="yellow"))
    if "<done></done>" in text:
        b_mode = False
        text = text.replace("<done></done>", "").strip()
        if not is_telegram: 
            console.print(Rule("✅ [bold green]PROJECT/TUGAS SELESAI[/bold green] ✅", style="green"))
            sys.stdout.write('\a'); sys.stdout.flush()
        if tg_state: tg_state["is_build_mode"] = b_mode
        else: is_build_mode = b_mode
        return "done", "Finished.", text, extracted_files, "done", {}
    
    if tg_state: tg_state["is_build_mode"] = b_mode
    else: is_build_mode = b_mode

    is_unclosed = False
    match = re.search(r'<([a-zA-Z_]+)([^>]*)>(.*?)</\1>', text, re.DOTALL)
    if not match:
        match = re.search(r'<([a-zA-Z_]+)([^>]*)>(.*)$', text, re.DOTALL)
        is_unclosed = True
    if not match: return False, None, text, extracted_files, None, {}

    tag = match.group(1)
    attrs_str = match.group(2)
    content = match.group(3).strip()
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', attrs_str))
    
    result_msg = ""
    original_full_tag_text = text 
    if is_unclosed: original_full_tag_text += f"</{tag}>"
    
    if config["safe_mode"] and not is_telegram and tag in ["cmd", "daemon", "write", "replace", "delete", "sql", "git", "screenshot", "refactor"]:
        safe_check = yes_no_dialog(title="🛡️ Security System Alert", text=f"Pica AI mencoba mengeksekusi aksi:\n\nTool: <{tag}>\nTarget: {content[:100]}...\n\nIzinkan eksekusi ini?", style=PICA_DIALOG_STYLE).run()
        if not safe_check: return True, f"EXECUTION BLOCKED BY USER IN SAFE MODE.", original_full_tag_text, extracted_files, tag, attrs

    try:
        if tag == "cmd":
            proc = subprocess.Popen(content, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = proc.communicate(timeout=10)
                cmd_output = stdout.strip() if stdout.strip() else stderr.strip()
                if not is_telegram: console.print(Panel(Syntax(fold_code(cmd_output), "bash", theme="monokai", word_wrap=True), title=f"💻 Command: {content}", border_style="cyan"))
                if proc.returncode != 0:
                    if not is_telegram: console.print(f"[{COLOR_ERROR}]⚠️ Error terdeteksi! Mengaktifkan Pica Auto-Heal...[/{COLOR_ERROR}]")
                    log_mistake(f"Command '{content}' returned error: {stderr}")
                    result_msg = f"ERROR. RETURN CODE: {proc.returncode}\nSTDERR:\n{stderr}\n\n[AUTO-DEBUG ACTIVATED] Analyze the error, fix files, and try again."
                else: result_msg = f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            except subprocess.TimeoutExpired: result_msg = "Command is running and took longer than 10s. Assuming success."
        
        elif tag == "daemon":
            log_path = os.path.join(LOGS_DIR, f"daemon_{int(time.time())}.log")
            log_file = open(log_path, "w")
            proc = subprocess.Popen(content, shell=True, stdout=log_file, stderr=subprocess.STDOUT)
            daemons[str(proc.pid)] = {"proc": proc, "log": log_path}
            result_msg = f"Daemon started with PID: {proc.pid}. View output using <read_log pid='{proc.pid}' lines='20'></read_log>"
            if not is_telegram: console.print(f"[{COLOR_SUCCESS}]⚙️ Daemon berjalan di Background (PID: {proc.pid}): {content}[/{COLOR_SUCCESS}]")
            
        elif tag == "read_log":
            pid = attrs.get("pid", "")
            lines_to_read = int(attrs.get("lines", 20))
            if pid in daemons:
                with open(daemons[pid]["log"], "r") as f:
                    lines = f.readlines()
                    log_content = "".join(lines[-lines_to_read:])
                if not is_telegram: console.print(Panel(fold_code(log_content), title=f"📄 Log Daemon (PID: {pid})", border_style="magenta"))
                result_msg = f"Log content for PID {pid}:\n{log_content}" if log_content else "Log is empty."
            else: result_msg = f"Error: PID {pid} is not a running daemon."

        elif tag == "replace":
            path = os.path.abspath(attrs.get("path"))
            old_str_match = re.search(r'<old>(.*?)</old>', content, re.DOTALL)
            new_str_match = re.search(r'<new>(.*?)</new>', content, re.DOTALL)
            if not old_str_match or not new_str_match: result_msg = "Error: Invalid <replace> format."
            else:
                old_str = old_str_match.group(1).strip('\n')
                new_str = new_str_match.group(1).strip('\n')
                with open(path, "r", encoding="utf-8") as f: file_content = f.read()
                if old_str in file_content:
                    backup_file(path)
                    new_file_content = file_content.replace(old_str, new_str, 1)
                    with open(path, "w", encoding="utf-8") as f: f.write(new_file_content)
                    if not is_telegram:
                        diff = list(difflib.unified_diff(old_str.splitlines(keepends=True), new_str.splitlines(keepends=True), fromfile="Original", tofile="Updated"))
                        console.print(Panel(Syntax(fold_code("".join(diff)), "diff", theme="monokai", background_color="default"), title=f"✂️ Smart Replace: {path}", border_style="yellow"))
                    result_msg = f"Successfully replaced code in {path}"
                else: 
                    log_mistake(f"Replace failed in {path}. <old> string not found.")
                    result_msg = f"Error: Exact string inside <old> NOT found in {path}."

        elif tag == "write":
            path = os.path.abspath(attrs.get("path", "output.txt"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            backup_file(path)
            with open(path, "w", encoding="utf-8") as f: f.write(content)
            result_msg = f"File {path} written successfully."
            if not is_telegram: console.print(Panel(Syntax(fold_code(content), path.split('.')[-1], theme="monokai", background_color="default"), title=f"📝 File Disimpan: {path}", border_style="green"))
            
        elif tag == "restore":
            path = os.path.abspath(attrs.get("path"))
            bname = os.path.basename(path)
            backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(bname)], reverse=True)
            if backups:
                shutil.copy2(os.path.join(BACKUP_DIR, backups[0]), path)
                if not is_telegram: console.print(f"[{COLOR_SUCCESS}]⏪ Time Machine: Berhasil mengembalikan {path}.[/{COLOR_SUCCESS}]")
                result_msg = f"Successfully restored {path} from backup."
            else: result_msg = f"Error: No backup found for {path}."

        elif tag == "refactor":
            path_dir = attrs.get("path", "."); ext = attrs.get("ext", ".js")
            old_str = attrs.get("old", ""); new_str = attrs.get("new", ""); count = 0
            if old_str:
                for root, _, files in os.walk(path_dir):
                    if "node_modules" in root or ".git" in root: continue
                    for f in files:
                        if f.endswith(ext):
                            fp = os.path.join(root, f)
                            try:
                                with open(fp, "r", encoding="utf-8") as file: cont = file.read()
                                if old_str in cont:
                                    backup_file(fp)
                                    with open(fp, "w", encoding="utf-8") as file: file.write(cont.replace(old_str, new_str))
                                    count += 1
                            except: pass
            result_msg = f"Mass refactored '{old_str}' to '{new_str}' in {count} files ending with {ext}."
            if not is_telegram: console.print(f"[{COLOR_SUCCESS}]♻️ Mass Refactor: {count} files modified.[/{COLOR_SUCCESS}]")

        elif tag == "tree":
            path_dir = attrs.get("path", ".")
            depth = int(attrs.get("depth", 2))
            tree_output = generate_tree(path_dir, max_depth=depth)
            if not is_telegram: console.print(Panel(tree_output, title=f"🌳 Folder Tree: {path_dir}", border_style="cyan"))
            result_msg = f"Directory tree:\n{tree_output}"

        elif tag == "http":
            method = attrs.get("method", "GET").upper()
            url_req = attrs.get("url")
            body_str = attrs.get("body", "{}")
            try:
                headers_req = {"Content-Type": "application/json"}
                body_json = json.loads(body_str) if body_str.strip() else None
                res = requests.request(method, url_req, json=body_json, headers=headers_req, timeout=10)
                result_msg = f"Status: {res.status_code}\nResponse: {res.text[:1500]}"
                if not is_telegram: console.print(Panel(result_msg[:500], title=f"🌐 HTTP {method} {url_req}", border_style="blue"))
            except Exception as e:
                result_msg = f"HTTP Error: {str(e)}"; log_mistake(f"HTTP to {url_req} failed: {str(e)}")

        elif tag == "search":
            if not is_telegram: console.print(f"[{COLOR_PRIMARY}]🔍 Mencari di Web: {content}[/{COLOR_PRIMARY}]")
            result_msg = perform_web_search(content)

        elif tag == "index_dir":
            path = attrs.get("path", "."); kw = attrs.get("keyword", "").lower(); matches = []
            for root, _, files in os.walk(path):
                if "node_modules" in root or ".git" in root: continue
                for file in files:
                    if file.endswith(('.py', '.html', '.js', '.css', '.txt', '.json', '.php')):
                        fp = os.path.join(root, file)
                        try:
                            with open(fp, "r", encoding="utf-8") as f:
                                if kw in f.read().lower(): matches.append(fp)
                        except: pass
            result_msg = f"Found keyword in files: {', '.join(matches)}" if matches else "Keyword not found."

        elif tag == "plan":
            with open(os.path.join(PICA_DIR, "project_plan.txt"), "w") as f: f.write(content)
            if not is_telegram: console.print(Panel(Markdown(content), title="📋 Project Blueprint", border_style="cyan"))
            result_msg = "Project plan saved."

        elif tag == "expose_port":
            port = content
            if not is_telegram: console.print(f"[{COLOR_SYSTEM}]🌐 Mengekspos Port {port} (Serveo)...[/{COLOR_SYSTEM}]")
            proc = subprocess.Popen(f"ssh -R 80:localhost:{port} nokey@localhost.run", shell=True, stdout=subprocess.PIPE, text=True)
            daemons[str(proc.pid)] = {"proc": proc, "log": os.devnull}
            time.sleep(3)
            result_msg = f"Port {port} has been exposed. (Background SSH started)."

        elif tag == "sys_info":
            info = get_system_stats()
            if not is_telegram: console.print(Panel(info, title="🖥️ System Monitor", border_style="magenta"))
            result_msg = info

        elif tag == "git":
            proc = subprocess.run(f"git {content}", shell=True, capture_output=True, text=True)
            result_msg = f"Git Result: {proc.stdout}\n{proc.stderr}"
            if not is_telegram: console.print(f"[{COLOR_SYSTEM}]🐙 Git Execute: {content}[/{COLOR_SYSTEM}]")

        elif tag == "sql":
            db_path = attrs.get("path", "database.sqlite")
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(content); conn.commit()
                rows = cur.fetchall(); conn.close()
                result_msg = f"SQL Result: {rows}" if rows else "Query executed successfully (No output)."
                if not is_telegram: console.print(f"[{COLOR_SUCCESS}]🛢️ SQL Query Dieksekusi di {db_path}[/{COLOR_SUCCESS}]")
            except Exception as e: result_msg = f"SQL Error: {str(e)}"

        elif tag == "delegate":
            if not is_telegram: console.print(f"[{COLOR_SYSTEM}]🤖 Memanggil Sub-Agent...[/{COLOR_SYSTEM}]")
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(sub_agent_task, content)
                res = future.result()
            result_msg = f"SUB-AGENT RESULT: {res}"

        elif tag == "zip":
            folder = attrs.get("path", ".")
            arch_name = content
            shutil.make_archive(arch_name, 'zip', folder)
            full_zip_path = os.path.abspath(f"{arch_name}.zip")
            extracted_files.append({"type": "document", "path": full_zip_path})
            result_msg = f"Successfully zipped {folder} into {arch_name}.zip"
            if not is_telegram: console.print(f"[{COLOR_SUCCESS}]🗜️ Zipped: {arch_name}.zip[/{COLOR_SUCCESS}]")

        elif tag == "image_gen":
            path = os.path.abspath(attrs.get("path", "image.png"))
            if not is_telegram: console.print(f"[{COLOR_PRIMARY}]🎨 Meng-generate Gambar: {content}[/{COLOR_PRIMARY}]")
            result_msg = generate_image(content, path)
            extracted_files.append({"type": "photo", "path": path})
            if not is_telegram: console.print(Panel(f"Prompt: {content}\nStatus: {result_msg}", title="🎨 Image Generated", border_style="magenta"))
            
        elif tag == "screenshot":
            target_url = attrs.get("url")
            device = attrs.get("device", "desktop")
            path = attrs.get("path", "screenshot.png")
            if not target_url: result_msg = "Error: <screenshot> requires a 'url' attribute."
            else:
                if not is_telegram: console.print(f"[{COLOR_PRIMARY}]📸 Mengambil Screenshot: {target_url} ({device})[/{COLOR_PRIMARY}]")
                result_msg = capture_screenshot(target_url, device, path)
                extracted_files.append({"type": "photo", "path": os.path.abspath(path)})
                if not is_telegram: console.print(Panel(f"URL Target: {target_url}\nStatus: {result_msg}", title="📸 Screenshot Berhasil", border_style="cyan"))

        elif tag == "quest":
            q_type = attrs.get("type", "input")
            if is_telegram: return "tg_quest_pending", content, original_full_tag_text, extracted_files, tag, attrs
            
            ans_str = ""
            try:
                if q_type == "yesno":
                    result = yes_no_dialog(title="❓ Konfirmasi Pica AI", text=content, style=PICA_DIALOG_STYLE).run()
                    ans_str = "Yes" if result else "No" if result is False else "Dibatalkan oleh user."
                elif q_type == "choice":
                    opts_raw = attrs.get("options", "").split(",")
                    opts = [(opt.strip(), opt.strip()) for opt in opts_raw if opt.strip()]
                    result = checkboxlist_dialog(title="❓ Pilihan Rekomendasi Pica AI", text=content, values=opts, style=PICA_DIALOG_STYLE).run()
                    ans_str = ", ".join(result) if result else "Tidak ada opsi yang dipilih."
                else:
                    result = input_dialog(title="❓ Pica Membutuhkan Detail", text=content, style=PICA_DIALOG_STYLE).run()
                    ans_str = result if result else "Dibatalkan oleh user."
            except KeyboardInterrupt: ans_str = "Dibatalkan oleh user (Ctrl+C)."
            return "quest_answered", ans_str, original_full_tag_text, extracted_files, tag, attrs

        elif tag == "todo":
            action = attrs.get("action", "list")
            if action == "create":
                tasks = [t.strip() for t in content.split('\n') if t.strip()]
                todos.clear()
                for task in tasks: todos.append({"task": task, "done": False})
                display_txt = "\n".join([f"[ ] {t['task']}" for t in todos])
                if not is_telegram: console.print(Panel(display_txt, title="📋 Todo List Dibuat", border_style="cyan"))
                result_msg = f"Todo list created successfully:\n{display_txt}"
            elif action == "checklist":
                target_task = content.strip().lower()
                found = False
                for t in todos:
                    if target_task in t["task"].lower():
                        t["done"] = True; found = True; break
                display_txt = "\n".join([f"[{'x' if t['done'] else ' '}] {t['task']}" for t in todos])
                if not is_telegram: console.print(Panel(display_txt, title="📋 Todo List Diperbarui", border_style="green"))
                result_msg = f"Task checked off. Current Todo:\n{display_txt}" if found else f"Task '{content}' not found."
            elif action == "list":
                if not todos: result_msg = "Todo list is empty."
                else:
                    display_txt = "\n".join([f"[{'x' if t['done'] else ' '}] {t['task']}" for t in todos])
                    if not is_telegram: console.print(Panel(display_txt, title="📋 Status Todo List Saat Ini", border_style="blue"))
                    result_msg = f"Current Todo:\n{display_txt}"
            else: result_msg = "Unknown todo action."
        else: result_msg = f"Unknown Tag executed."

    except Exception as e:
        result_msg = f"Execution Error ({tag}): {str(e)}"
        if not is_telegram: console.print(f"[{COLOR_ERROR}]Execution Error ({tag}): {str(e)}[/{COLOR_ERROR}]")
        
    return True, result_msg, original_full_tag_text, extracted_files, tag, attrs

# --- API LOADING ANIMATION CLI ---
def fetch_response_with_progress(messages):
    response_text = None
    with Progress(
        SpinnerColumn(spinner_name="dots2", style="bold yellow"),
        TextColumn("[bold yellow]Pica AI sedang menganalisa sistem...[/bold yellow]"),
        TimeElapsedColumn(), transient=True, console=console
    ) as progress:
        task = progress.add_task("Processing...", total=None)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(call_pica_api, messages)
            response_text = future.result()
    return response_text

# --- MAIN CLI AGENT LOOP ---
def run_agent(user_input, session, is_image=False):
    global chat_history, is_build_mode
    if is_image:
        chat_history.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": user_input}}, {"type": "text", "text": "Tolong analisa gambar ini."}]})
        console.print(Panel("🖼️ Gambar dikirim ke AI.", title="🧑 Anda", border_style=COLOR_USER))
    else:
        chat_history.append({"role": "user", "content": user_input})
        console.print(Panel(Text(user_input), title="🧑 [bold cyan]Anda[/bold cyan]", title_align="left", border_style=COLOR_USER))
    
    while True:
        response_text = fetch_response_with_progress(chat_history)
        if response_text == "ERROR_TOKEN":
            console.print(Panel("Akses Ditolak. Gunakan perintah [bold]/token pica-xxxx[/bold]", style=COLOR_ERROR))
            break
        elif str(response_text).startswith("ERROR_API"):
            console.print(Panel(response_text, style=COLOR_ERROR))
            break

        action_type, result, corrected_response, files, tag, attrs = parse_and_execute(response_text, session)
        chat_history.append({"role": "assistant", "content": corrected_response})
        
        clean_text = re.sub(r'<([a-zA-Z_]+)[^>]*>.*', '', corrected_response, flags=re.DOTALL).strip()
        if clean_text and not is_build_mode:
            console.print(Panel(Markdown(clean_text), title="🤖 [bold yellow]Pica AI[/bold yellow]", title_align="left", border_style=COLOR_PRIMARY))

        if action_type == "done":
            save_session_state(); break
        elif action_type == "quest_answered":
            console.print(Panel(Syntax(result, "markdown", theme="monokai", background_color="default"), title="🧑 [bold cyan]Jawaban Terkirim[/bold cyan]", border_style=COLOR_USER))
            chat_history.append({"role": "user", "content": f"My answer is:\n{result}\nProceed using this information."})
            save_session_state()
        elif action_type is True:
            chat_history.append({"role": "user", "content": f"System Result:\n{result}\nContinue execution."})
            save_session_state()
        else: 
            save_session_state(); break 

# --- V12.2: TELEGRAM NATIVE API FUNCTIONS ---
def tg_send_message(bot_token, chat_id, text, reply_markup=None):
    if not text.strip(): return
    for i in range(0, len(text), 4000): 
        payload = {"chat_id": chat_id, "text": text[i:i+4000], "parse_mode": "Markdown"}
        if reply_markup: payload["reply_markup"] = reply_markup
        requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload)

def tg_send_photo(bot_token, chat_id, photo_path, caption=""):
    try:
        with open(photo_path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{bot_token}/sendPhoto", data={"chat_id": chat_id, "caption": caption}, files={"photo": f})
    except: pass

def tg_send_document(bot_token, chat_id, doc_path, caption=""):
    try:
        with open(doc_path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{bot_token}/sendDocument", data={"chat_id": chat_id, "caption": caption}, files={"document": f})
    except: pass

def tg_set_commands(bot_token):
    commands = [
        {"command": "start", "description": "Mulai / Bangungkan Pica AI"},
        {"command": "clear", "description": "Hapus memori AI (Reset Sesi)"},
        {"command": "status", "description": "Cek Todo List dan Mode Pica"}
    ]
    requests.post(f"https://api.telegram.org/bot{bot_token}/setMyCommands", json={"commands": commands})

# --- V12.2: TELEGRAM SWARM SYSTEM ---
def telegram_run_agent(bot_token, admin_id, input_text, img_b64, tg_state):
    
    if tg_state.get("pending_quest"):
        tg_state["chat_history"].append({"role": "user", "content": f"My answer is: {input_text}. Please proceed."})
        tg_state["pending_quest"] = False
    else:
        if img_b64:
            img_url = f"data:image/jpeg;base64,{img_b64}"
            tg_state["chat_history"].append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": img_url}}, {"type": "text", "text": input_text or "Analisa gambar ini."}]})
        else:
            tg_state["chat_history"].append({"role": "user", "content": input_text})

    requests.post(f"https://api.telegram.org/bot{bot_token}/sendChatAction", json={"chat_id": admin_id, "action": "typing"})
    
    while True:
        response_text = call_pica_api(tg_state["chat_history"], tg_state=tg_state)
        
        if str(response_text).startswith("ERROR_"):
            tg_send_message(bot_token, admin_id, f"❌ *API Error:* {response_text}")
            break

        action_type, result, corrected_response, extracted_files, tag, attrs = parse_and_execute(response_text, session=None, is_telegram=True, tg_state=tg_state)
        tg_state["chat_history"].append({"role": "assistant", "content": corrected_response})
        
        clean_text = re.sub(r'<([a-zA-Z_]+)[^>]*>.*', '', corrected_response, flags=re.DOTALL).strip()
        
        if clean_text: tg_send_message(bot_token, admin_id, f"🤖 *PICA:*\n{clean_text}")
        
        if tag == "cmd": tg_send_message(bot_token, admin_id, f"💻 *Executing Command:*\n`{attrs.get('content','')[:50]}...`")
        elif tag == "write": tg_send_message(bot_token, admin_id, f"📝 *Menulis File:*\n`{attrs.get('path','')}`")
        elif tag == "daemon": tg_send_message(bot_token, admin_id, f"⚙️ *Menjalankan Server (Daemon):*\n`{attrs.get('content','')}`")
        elif tag == "todo" and attrs.get('action') == "checklist": 
            tg_send_message(bot_token, admin_id, f"✅ *Todo Selesai:*\n_{attrs.get('content','')}_")

        if extracted_files:
            for file_dict in extracted_files:
                if file_dict["type"] == "photo":
                    tg_send_photo(bot_token, admin_id, file_dict["path"], caption="📸 Result from Pica AI")
                elif file_dict["type"] == "document":
                    tg_send_document(bot_token, admin_id, file_dict["path"], caption="📦 Project Zip File Generated by Pica AI")

        if action_type == "done":
            tg_send_message(bot_token, admin_id, "🎉 *Task Fully Completed!*")
            break
        elif action_type == "tg_quest_pending":
            q_type = attrs.get("type", "input")
            if q_type == "yesno":
                markup = {"inline_keyboard": [[{"text": "✅ Yes", "callback_data": "Yes"}, {"text": "❌ No", "callback_data": "No"}]]}
                tg_send_message(bot_token, admin_id, f"❓ *PICA BERTANYA:*\n{result}", reply_markup=markup)
            elif q_type == "choice":
                opts = attrs.get("options", "").split(",")
                keys = [[{"text": o.strip(), "callback_data": o.strip()}] for o in opts if o.strip()]
                markup = {"inline_keyboard": keys}
                tg_send_message(bot_token, admin_id, f"❓ *PICA BERTANYA:*\n{result}\n\n_(Pilih salah satu)_", reply_markup=markup)
            else:
                tg_send_message(bot_token, admin_id, f"⌨️ *PICA BERTANYA:*\n{result}\n\n_(Balas pesan ini untuk menjawab)_")
            
            tg_state["pending_quest"] = True
            break 

        elif action_type is True:
            tg_state["chat_history"].append({"role": "user", "content": f"System Result:\n{result}\nContinue execution."})
            requests.post(f"https://api.telegram.org/bot{bot_token}/sendChatAction", json={"chat_id": admin_id, "action": "typing"})
        else: break

def telegram_poller(bot_config):
    bot_token = bot_config["token"]
    admin_id = str(bot_config["admin"])
    offset = 0
    tg_state = {"chat_history": [], "project_todos": [], "is_build_mode": False, "token_estimate": 0, "pending_quest": False}

    tg_set_commands(bot_token) 

    while True:
        try:
            res = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={offset}&timeout=30", timeout=40).json()
            if res.get("ok"):
                for item in res["result"]:
                    offset = item["update_id"] + 1
                    
                    if "callback_query" in item:
                        cq = item["callback_query"]
                        chat_id = str(cq["message"]["chat"]["id"])
                        if chat_id != admin_id: continue
                        
                        input_text = cq["data"]
                        requests.get(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery?callback_query_id={cq['id']}")
                        requests.post(f"https://api.telegram.org/bot{bot_token}/editMessageText", json={
                            "chat_id": chat_id, "message_id": cq["message"]["message_id"], 
                            "text": f"{cq['message']['text']}\n\n*Anda menjawab:* {input_text}", "parse_mode": "Markdown"
                        })
                        threading.Thread(target=telegram_run_agent, args=(bot_token, admin_id, input_text, None, tg_state), daemon=True).start()
                        continue

                    msg = item.get("message")
                    if not msg: continue
                    
                    chat_id = str(msg["chat"]["id"])
                    if chat_id != admin_id: continue 
                    
                    text = msg.get("text", "")
                    photo = msg.get("photo")
                    caption = msg.get("caption", "")
                    input_text = text or caption
                    
                    if text == "/start":
                        tg_send_message(bot_token, admin_id, "🚀 *Pica AI Online!* Saya siap menerima perintah Anda.")
                        continue
                    elif text == "/clear":
                        tg_state["chat_history"].clear(); tg_state["project_todos"].clear(); tg_state["is_build_mode"] = False
                        tg_send_message(bot_token, admin_id, "🧹 *Memori dibersihkan!* Sesi Telegram direset.")
                        continue
                    elif text == "/status":
                        mode = "🛠️ BUILD" if tg_state["is_build_mode"] else "💬 CHAT"
                        todos = "\n".join([f"[{'x' if t['done'] else ' '}] {t['task']}" for t in tg_state["project_todos"]]) if tg_state["project_todos"] else "Kosong."
                        tg_send_message(bot_token, admin_id, f"*Status Pica:*\nMode: {mode}\nTodo List:\n{todos}")
                        continue

                    img_b64 = None
                    if photo:
                        file_id = photo[-1]["file_id"] 
                        f_res = requests.get(f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}").json()
                        file_path = f_res["result"]["file_path"]
                        img_data = requests.get(f"https://api.telegram.org/file/bot{bot_token}/{file_path}").content
                        img_b64 = base64.b64encode(img_data).decode('utf-8')
                        
                    threading.Thread(target=telegram_run_agent, args=(bot_token, admin_id, input_text, img_b64, tg_state), daemon=True).start()
        except Exception: time.sleep(5)

# --- START TELEGRAM BOTS ---
def start_all_telegram_bots():
    bots = config.get("telegram_bots", [])
    for b in bots: threading.Thread(target=telegram_poller, args=(b,), daemon=True).start()

start_all_telegram_bots()

# --- CLI COMMAND HANDLERS ---
class PicaCommandValidator(Validator):
    def validate(self, document):
        text = document.text.strip()
        if text.startswith('/'):
            cmd = text.split()[0]
            valid_cmds = ['/help', '/token', '/web_gen', '/safe_mode', '/kill', '/upload', '/macro', '/m', '/session', '/vi_mode', '/init', '/find', '/context', '/telegram']
            if cmd not in valid_cmds:
                raise ValidationError(message=f"Oops! Perintah '{cmd}' tidak ditemukan.", cursor_position=len(cmd))
            needs_arg = ['/token', '/upload', '/m', '/web_gen', '/safe_mode', '/kill', '/macro', '/session', '/vi_mode', '/init', '/find', '/context', '/telegram']
            if cmd in needs_arg and len(text.split()) < 2:
                raise ValidationError(message=f"Perintah '{cmd}' butuh argumen tambahan!", cursor_position=len(text))

def handle_commands(cmd_str, session):
    global chat_history, project_todos, is_build_mode 
    
    parts = cmd_str.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        table = Table(title="🌟 Daftar Perintah Pica AI V12.2", style=COLOR_PRIMARY)
        cmds = [
            ("/token <token>", "Setup Token API"),
            ("/telegram <token> <id>", "Sambung ke Bot Telegram"),
            ("/init <html/node>", "Generate Project Boilerplate"),
            ("/find <keyword>", "Semantic Code Search di Folder"),
            ("/context <clear/angka>", "Manage Ukuran Memori AI"),
            ("/safe_mode <on/off>", "Toggle perlindungan OS"),
            ("/vi_mode <on/off>", "Aktifkan Editor Vim di Input"),
            ("/kill <pid>", "Matikan proses Daemon background"),
            ("/upload <path>", "Unggah Gambar Lokal"),
            ("/macro <nama> <teks>", "Buat Shortcut Perintah"),
            ("/m <nama>", "Jalankan Shortcut Macro"),
            ("/session <new/clear>", "Manajemen Sesi Obrolan")
        ]
        for c, d in cmds: table.add_row(c, d)
        console.print(table)

    elif cmd == "/telegram":
        try:
            bot_token, admin_id = args.split()
            bot_conf = {"token": bot_token, "admin": admin_id}
            if "telegram_bots" not in config: config["telegram_bots"] = []
            config["telegram_bots"].append(bot_conf)
            save_json(CONFIG_FILE, config)
            threading.Thread(target=telegram_poller, args=(bot_conf,), daemon=True).start()
            console.print(f"[{COLOR_SUCCESS}]✔ Berhasil! Bot Telegram siap menerima pesan dari ID {admin_id}.[/{COLOR_SUCCESS}]")
        except: console.print(f"[{COLOR_ERROR}]Format salah! Gunakan: /telegram <bot_token> <id_admin>[/{COLOR_ERROR}]")

    elif cmd == "/token":
        config["token"] = args; save_json(CONFIG_FILE, config)
        console.print(f"[{COLOR_SUCCESS}]✔ Token tersimpan![/{COLOR_SUCCESS}]")
        
    elif cmd == "/vi_mode":
        config["vi_mode"] = (args.lower() == "on"); save_json(CONFIG_FILE, config)
        console.print(f"[{COLOR_SUCCESS}]✔ Vi/Vim Mode {'AKTIF' if config['vi_mode'] else 'MATI'}. Restart skrip untuk menerapkan.[/{COLOR_SUCCESS}]")

    elif cmd == "/safe_mode":
        config["safe_mode"] = (args.lower() == "on"); save_json(CONFIG_FILE, config)
        console.print(f"[{COLOR_SUCCESS}]✔ Safe Mode {'AKTIF' if config['safe_mode'] else 'MATI'}.[/{COLOR_SUCCESS}]")

    elif cmd == "/kill":
        try:
            pid = args.strip()
            if pid in daemons:
                daemons[pid]["proc"].terminate(); del daemons[pid]
                console.print(f"[{COLOR_SUCCESS}]✔ Daemon PID {pid} dimatikan.[/{COLOR_SUCCESS}]")
            else:
                os.kill(int(pid), 9)
                console.print(f"[{COLOR_SUCCESS}]✔ Proses {pid} dimatikan paksa OS.[/{COLOR_SUCCESS}]")
        except Exception as e: console.print(f"[{COLOR_ERROR}]Gagal mematikan proses: {e}[/{COLOR_ERROR}]")

    elif cmd == "/session":
        sub = args.split(); act = sub[0] if sub else ""
        if act == "new":
            chat_history.clear(); project_todos.clear(); is_build_mode = False
            config["current_session"] = f"sess_{int(time.time())}"; save_json(CONFIG_FILE, config)
            save_session_state() 
            console.print(f"[{COLOR_SUCCESS}]✔ Sesi baru siap digunakan.[/{COLOR_SUCCESS}]")
        elif act == "clear":
            chat_history.clear(); project_todos.clear(); is_build_mode = False
            save_session_state()
            console.print(f"[{COLOR_SUCCESS}]✔ Memori sesi ini telah dibersihkan.[/{COLOR_SUCCESS}]")

    elif cmd == "/context":
        if args == "clear":
            chat_history.clear(); console.print(f"[{COLOR_SUCCESS}]✔ Context History dibersihkan.[/{COLOR_SUCCESS}]")
        elif args.isdigit():
            keep = int(args)
            if len(chat_history) > keep: chat_history[:] = chat_history[-keep:]
            console.print(f"[{COLOR_SUCCESS}]✔ Context dipotong menjadi {keep} pesan terakhir.[/{COLOR_SUCCESS}]")
        else: console.print(f"[{COLOR_ERROR}]Gunakan: /context clear ATAU /context <angka>[/{COLOR_ERROR}]")

    elif cmd == "/find":
        kw = args.lower(); matches = []
        for root, _, files in os.walk("."):
            if "node_modules" in root or ".git" in root: continue
            for file in files:
                if file.endswith(('.py', '.html', '.js', '.css', '.txt', '.php')):
                    fp = os.path.join(root, file)
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                            for i, line in enumerate(lines):
                                if kw in line.lower(): matches.append(f"{fp}:{i+1} -> {line.strip()[:100]}")
                    except: pass
        if matches:
            console.print(Panel("\n".join(matches[:20]), title=f"🔍 Find Results for '{kw}'", border_style="cyan"))
            if len(matches) > 20: console.print(f"...and {len(matches)-20} more.")
        else: console.print(f"[{COLOR_ERROR}]Tidak ada hasil untuk '{kw}'.[/{COLOR_ERROR}]")

    elif cmd == "/init":
        if args == "html":
            os.makedirs("css", exist_ok=True); os.makedirs("js", exist_ok=True)
            with open("index.html", "w") as f: f.write("<!DOCTYPE html>\n<html>\n<head>\n<title>App</title>\n<link rel='stylesheet' href='css/style.css'>\n</head>\n<body>\n<h1>Hello Pica</h1>\n<script src='js/script.js'></script>\n</body>\n</html>")
            with open("css/style.css", "w") as f: f.write("body { font-family: sans-serif; background: #111; color: white; display: flex; justify-content: center; padding-top: 50px; }")
            with open("js/script.js", "w") as f: f.write("console.log('Pica App Ready');")
            console.print(f"[{COLOR_SUCCESS}]✔ Template HTML/CSS/JS Boilerplate berhasil dibuat![/{COLOR_SUCCESS}]")
        elif args == "node":
            with open("package.json", "w") as f: f.write('{"name":"app","version":"1.0.0","main":"index.js","scripts":{"start":"node index.js"}}')
            with open("index.js", "w") as f: f.write("const http = require('http');\nhttp.createServer((req, res) => { res.end('Hello Node from Pica'); }).listen(3000, () => console.log('Server running on 3000'));")
            console.print(f"[{COLOR_SUCCESS}]✔ Template Node.js Boilerplate berhasil dibuat![/{COLOR_SUCCESS}]")
        else: console.print(f"[{COLOR_ERROR}]Template tidak ditemukan. Tersedia: html, node[/{COLOR_ERROR}]")

    elif cmd == "/upload":
        if os.path.exists(args):
            with open(args, "rb") as img:
                b64 = base64.b64encode(img.read()).decode('utf-8')
                mime = "image/jpeg" if args.endswith('.jpg') else "image/png"
                run_agent(f"data:{mime};base64,{b64}", session=session, is_image=True)
        else: console.print(f"[{COLOR_ERROR}]File gambar tidak ditemukan.[/{COLOR_ERROR}]")

    elif cmd == "/macro":
        sub = args.split(maxsplit=1)
        if len(sub) == 2:
            config["macros"][sub[0]] = sub[1]; save_json(CONFIG_FILE, config)
            console.print(f"[{COLOR_SUCCESS}]✔ Macro '{sub[0]}' disimpan.[/{COLOR_SUCCESS}]")

    elif cmd == "/m":
        if args in config["macros"]:
            console.print(f"[{COLOR_SYSTEM}]⚡ Menjalankan Macro: {args}[/{COLOR_SYSTEM}]")
            run_agent(config["macros"][args], session=session)

# --- UI STARTUP & TOOLBAR ---
def show_logo():
    console.clear()
    logo = """
   ██████╗ ██╗ ██████╗ █████╗     █████╗ ██╗
   ██╔══██╗██║██╔════╝██╔══██╗   ██╔══██╗██║
   ██████╔╝██║██║     ███████║   ███████║██║
   ██╔═══╝ ██║██║     ██╔══██║   ██╔══██║██║
   ██║     ██║╚██████╗██║  ██║██╗██║  ██║██║
   ╚═╝     ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚═╝
    """
    console.print(logo, style=COLOR_ACCENT, justify="center")
    
    if config.get("telegram_bots"):
        console.print(f"[{COLOR_SUCCESS}]📡 {len(config['telegram_bots'])} Bot Telegram sedang berjalan di background![/{COLOR_SUCCESS}]", justify="center")
        
    console.print(Panel(Align.center(f"✨ [bold yellow]Pica AI V12.2 | Mobile Friendly Update[/bold yellow] ✨\n"
                                     f"Ketik [bold cyan]/help[/bold cyan] untuk perintah.\n"
                                     f"Tips: Tekan [bold cyan]Enter 2x[/bold cyan] untuk mengirim pesan!"), 
                        border_style="yellow", padding=(1, 2)))

def get_bottom_toolbar():
    mode = " 🛠️ BUILD " if is_build_mode else " 💬 CHAT "
    bg_mode = "ansired" if is_build_mode else "ansiyellow"
    safe = " 🛡️ SAFE ON " if config["safe_mode"] else " 🔓 SAFE OFF "
    bg_safe = "ansigreen" if config["safe_mode"] else "ansigray"
    
    return HTML(
        f'<style bg="{bg_mode}" fg="black"><b>{mode}</b></style>'
        f'<style bg="{bg_safe}" fg="{bg_mode}"></style>'
        f'<style bg="{bg_safe}" fg="black"><b>{safe}</b></style>'
        f'<style bg="ansiblue" fg="{bg_safe}"></style>'
        f'<style bg="ansiblue" fg="white"> 🪙 Token Est: ~{token_estimate} </style>'
        f'<style bg="default" fg="ansiblue"></style>'
    )

def get_rprompt():
    return HTML(f'<style fg="ansicyan">⚡ Daemons: {len(daemons)} | 🧠 Sesi: {config["current_session"]}</style>')

# --- MAIN LOOP ---
def main():
    show_logo()

    bindings = KeyBindings()
    
    # Tetap pertahankan ESC+Enter untuk pengguna PC/Laptop yang sudah terbiasa
    @bindings.add('escape', 'enter')
    def _(event): event.current_buffer.validate_and_handle()

    # FIX V12.2: Deteksi tombol ENTER untuk Pengguna Mobile (Replit/Web/HP)
    @bindings.add('enter')
    def _(event):
        buffer = event.current_buffer
        text = buffer.document.text
        
        # 1. Jika ini perintah CLI pendek (cth: /help, /status), langsung kirim!
        if text.startswith('/') and '\n' not in text:
            buffer.validate_and_handle()
        # 2. Jika user menekan ENTER 2 KALI berturut-turut (baris terakhir kosong) -> KIRIM!
        elif text.endswith('\n'):
            buffer.delete_before_cursor(1) # Hapus newline lebihan biar rapi
            buffer.validate_and_handle()
        # 3. Jika baru Enter 1 kali, turun ke baris baru (Multiline mode)
        else:
            buffer.insert_text('\n')

    pica_completer = NestedCompleter.from_nested_dict({
        '/help': None, '/token': None, '/telegram': None,
        '/init': {'html': None, 'node': None},
        '/find': None,
        '/context': {'clear': None, '10': None, '20': None},
        '/safe_mode': {'on': None, 'off': None},
        '/vi_mode': {'on': None, 'off': None},
        '/kill': None, '/upload': PathCompleter(), 
        '/macro': None, '/m': None,
        '/session': {'new': None, 'list': None, 'load': None, 'clear': None}
    })

    session = PromptSession(
        message=HTML('<b><ansiyellow>Pica ❯ </ansiyellow></b>'),
        rprompt=get_rprompt,
        key_bindings=bindings,
        multiline=True,
        prompt_continuation=lambda width, line_number, is_soft_wrap: " " * 7, 
        history=FileHistory(HISTORY_FILE),
        auto_suggest=AutoSuggestFromHistory(),
        completer=pica_completer,
        validator=PicaCommandValidator(),        
        validate_while_typing=True,
        lexer=PygmentsLexer(MarkdownLexer),      
        bottom_toolbar=get_bottom_toolbar,
        vi_mode=config.get("vi_mode", False),    
        complete_while_typing=True
    )

    while True:
        try:
            user_input_raw = session.prompt()
            
            term_cols = shutil.get_terminal_size().columns
            lines_up = 0
            for line in user_input_raw.split('\n'):
                length = len(line) + 7 
                lines_up += (length // term_cols) + 1
            
            lines_up = min(lines_up, shutil.get_terminal_size().lines - 2)
            sys.stdout.write(f"\033[{lines_up}A\033[J")
            sys.stdout.flush()

            user_input = user_input_raw.strip()

            if not user_input: continue

            if user_input.startswith("/"): handle_commands(user_input, session)
            else: run_agent(user_input, session=session)

        except KeyboardInterrupt:
            save_session_state()
            console.print(f"\n[{COLOR_ERROR}]✖ Eksekusi Dibatalkan (Ctrl+C).[/{COLOR_ERROR}]")
            global is_build_mode; is_build_mode = False
        except EOFError:
            save_session_state()
            console.print(f"\n[{COLOR_SUCCESS}]✔ Sesi otomatis tersimpan. Sampai jumpa![/{COLOR_SUCCESS}]")
            break
        except Exception as e:
            console.print(f"\n[{COLOR_ERROR}]✖ System Error Terjadi: {str(e)}[/{COLOR_ERROR}]")

if __name__ == "__main__":
    main()
