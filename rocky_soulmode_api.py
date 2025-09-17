# rocky_soulmode_api.py
import os
import re
import sys
import json
import logging
import unittest
from datetime import datetime
from typing import List, Optional, Dict, Any

"""
=========================================
🚀 Rocky Soulmode Configuration (ENV Vars)
=========================================

# Core Switches
ROCKY_ALLOW_SERVER=1        # Allow FastAPI server to start
ROCKY_AUTONOMOUS=1          # Enable autonomous background worker
ROCKY_WORKER_INTERVAL=300   # Worker interval in seconds (default: 300 = 5 minutes)

# Behavior
ROCKY_AUTO_REPLY=1          # If set, Rocky auto-sends replies to /agent/{account}
ROCKY_USE_LLM=1             # Allow escalation to OpenAI LLM if local strategies fail
ROCKY_NOTIFY_WEBHOOK=<url>  # Optional: send replies/alerts to Discord/Slack via webhook

# Personality & Memory
ROCKY_SYNC_PERSONALITY=1    # Ensure every account always has a default personality baseline
ROCKY_SELF_REFLECT=1        # Rocky periodically reviews its own replies & saves reflections
ROCKY_MEMORY_TTL=90d        # Archive memories older than N days (e.g., "90d")

# Database
ROCKY_DB="rocky_soulmode"   # Default DB name

# OpenAI
OPENAI_API_KEY="sk-..."     # Enable GPT-based escalation

-----------------------------------------
Usage:
    export ROCKY_ALLOW_SERVER=1
    export ROCKY_AUTONOMOUS=1
    python rocky_soulmode_api.py
-----------------------------------------
"""

# Feature detection
HAS_FASTAPI = False
HAS_PYDANTIC = False
HAS_OPENAI = False

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field
    HAS_FASTAPI = True
    HAS_PYDANTIC = True
except Exception:
    HAS_FASTAPI = False

"""
⚠️ NOTE: Firestore Security Rules are managed in the Firebase console, not here.
Make sure the service account used has `Cloud Datastore User` or `Cloud Firestore User` roles.
"""

# Firestore
from google.cloud import firestore
from google.oauth2 import service_account

try:
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.exists(cred_path):
        creds = service_account.Credentials.from_service_account_file(
            cred_path,
            scopes=["https://www.googleapis.com/auth/datastore"]
        )
        firestore_client = firestore.Client(credentials=creds, project=creds.project_id)
        print(f"🔥 Connected to Firestore project: {creds.project_id}")
    else:
        raise FileNotFoundError("Service account JSON not found or GOOGLE_APPLICATION_CREDENTIALS not set")
except Exception as e:
    firestore_client = None
    print("⚠️ Firestore not available, falling back to local memory:", e)

# OpenAI
try:
    import openai
    if os.getenv("OPENAI_API_KEY"):
        openai.api_key = os.getenv("OPENAI_API_KEY")
        HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rocky_soulmode")

# Local fallback storage
DB_NAME = os.getenv("ROCKY_DB", "rocky_soulmode")
_local_memory: Dict[str, Dict[str, Any]] = {}   # account -> key -> doc
_local_threads: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}  # account -> thread_id -> messages

# ----------------- Utilities -----------------
def now_iso() -> str:
    return datetime.utcnow().isoformat()

def tokenize(text: Optional[str]) -> List[str]:
    if not text:
        return []
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return [t for t in text.split() if len(t) > 1]

def extractive_summary(messages: List[Dict[str, Any]], max_sentences: int = 3) -> str:
    combined = " ".join(m.get("content", "") for m in messages)
    if not combined.strip():
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', combined)
    tokens = tokenize(combined)
    freq: Dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    scored: List[tuple] = []
    for s in sentences:
        s_tokens = tokenize(s)
        score = sum(freq.get(w, 0) for w in s_tokens)
        scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [s for _, s in scored[:max_sentences]]
    ordered = [s for s in sentences if s in top]
    return " ".join(ordered).strip()

# ----------------- Storage operations -----------------
def _ensure_account(account: Optional[str]):
    acc = account or "global"
    if acc not in _local_memory:
        _local_memory[acc] = {}
    if acc not in _local_threads:
        _local_threads[acc] = {}
    return acc

def remember_data(account: Optional[str], key: str, value: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    acc = _ensure_account(account)
    doc = {
        "account": acc,
        "key": key,
        "value": value,
        "tags": tags or [],
        "timestamp": now_iso()
    }
    if firestore_client:
        try:
            firestore_client.collection("memories").document(acc).collection("items").document(key).set(doc)
            logger.info(f"[FIRESTORE] Saved memory {acc}:{key}")
        except Exception as e:
            logger.warning(f"[FIRESTORE] Save failed for {acc}:{key}: {e}")
    _local_memory[acc][key] = doc
    return doc


def recall_data(account: Optional[str], key: str) -> Optional[Dict[str, Any]]:
    acc = account or "global"
    if firestore_client:
        try:
            snap = firestore_client.collection("memories").document(acc).collection("items").document(key).get()
            if snap.exists:
                return snap.to_dict()
        except Exception as e:
            logger.warning(f"[FIRESTORE] Recall failed for {acc}:{key}: {e}")
    return _local_memory.get(acc, {}).get(key)


def forget_data(account: Optional[str], key: str) -> bool:
    acc = account or "global"
    removed = False
    if firestore_client:
        try:
            firestore_client.collection("memories").document(acc).collection("items").document(key).delete()
            removed = True
        except Exception as e:
            logger.warning(f"[FIRESTORE] Delete failed for {acc}:{key}: {e}")
    if _local_memory.get(acc, {}).pop(key, None) is not None:
        removed = True
    return removed


def export_all(account: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"memories": {}, "threads": {}, "personality": {}}
    acc_filter = account or None
    if firestore_client:
        try:
            if acc_filter:
                docs = firestore_client.collection("memories").document(acc_filter).collection("items").stream()
                for d in docs:
                    data = d.to_dict()
                    out["memories"][f"{acc_filter}::{data['key']}"] = data
            else:
                # fetch all accounts
                acc_docs = firestore_client.collection("memories").stream()
                for acc_doc in acc_docs:
                    acc_id = acc_doc.id
                    docs = firestore_client.collection("memories").document(acc_id).collection("items").stream()
                    for d in docs:
                        data = d.to_dict()
                        out["memories"][f"{acc_id}::{data['key']}"] = data
        except Exception as e:
            logger.warning(f"[EXPORT] Firestore memories export failed: {e}")
    # also include local fallback
    if acc_filter:
        out["memories"].update({f"{acc_filter}::{k}": v for k, v in _local_memory.get(acc_filter, {}).items()})
    else:
        for a, mems in _local_memory.items():
            out["memories"].update({f"{a}::{k}": v for k, v in mems.items()})
    return out

# ----------------- Thread operations -----------------
def log_thread(account: Optional[str], thread_id: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    acc = _ensure_account(account)
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", ""), "timestamp": m.get("timestamp") or now_iso()} for m in messages]
    if firestore_client:
        try:
            firestore_client.collection("threads").document(acc).collection("items").document(thread_id).set({
                "account": acc,
                "thread_id": thread_id,
                "messages": msgs,
                "timestamp": now_iso()
            })
            logger.info(f"[FIRESTORE] Saved thread {acc}:{thread_id}")
        except Exception as e:
            logger.warning(f"[FIRESTORE] Thread log failed for {acc}:{thread_id}: {e}")
    _local_threads[acc][thread_id] = msgs
    return {"account": acc, "thread_id": thread_id, "messages": msgs}

def fetch_thread_messages(account: Optional[str], thread_id: str) -> List[Dict[str, Any]]:
    acc = account or "global"
    if firestore_client:
        try:
            snap = firestore_client.collection("threads").document(acc).collection("items").document(thread_id).get()
            if snap.exists:
                return snap.to_dict().get("messages", [])
        except Exception as e:
            logger.warning(f"[THREAD] Firestore fetch failed for {acc}:{thread_id}: {e}")
    return _local_threads.get(acc, {}).get(thread_id, [])

# ----------------- Scan & Respond -----------------

def scan_and_respond(account: Optional[str], thread_id: Optional[str], query: Optional[str], max_context: int = 10, use_llm: bool = False) -> Dict[str, Any]:
    acc = account or "global"
    messages: List[Dict[str, Any]] = []
    if thread_id:
        messages = fetch_thread_messages(acc, thread_id)
    else:
        for msgs in _local_threads.get(acc, {}).values():
            messages.extend(msgs)
        if firestore_client:
            try:
                docs = firestore_client.collection("threads").where("account", "==", acc).stream()
                for d in docs:
                    messages.extend(d.to_dict().get("messages", []))
            except Exception as e:
                logger.warning(f"[SCAN] Firestore fetch failed: {e}")
    if query:
        messages = [m for m in messages if re.search(query, m.get("content", ""), re.I)]
    context = messages[-max_context:]
    summary = extractive_summary(context, max_sentences=4)
    suggested = ""
    if use_llm and HAS_OPENAI:
        try:
            prompt = f"Summary:\n{summary}\n\nUser Query: {query or ''}\n"
            resp = openai.ChatCompletion.create(model="gpt-4o-mini", messages=[{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": prompt}], max_tokens=300, temperature=0.2)
            suggested = resp["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"[LLM] OpenAI call failed: {e}")
    if not suggested:
        if query:
            suggested = f"Based on the chat summary: {summary}. Suggestion: answer the query directly and confirm next steps."
        else:
            latest_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if latest_user:
                suggested = f"Reply to user's latest message: '{latest_user.get('content')[:280]}' — acknowledge and provide next action."
            else:
                suggested = f"No user messages found. Summary: {summary or 'none'}."
    return {"summary": summary, "suggested_reply": suggested, "scanned_count": len(messages)}

# ----------------- Personality helpers -----------------
DEFAULT_PERSONALITY = {
    "tone": "consistent",
    "style": "cofounder-high-energy",
    "signature": "💎🔥",
    "include_oob": True,
    "thinking": "out-of-box"
}

def get_personality(account: Optional[str]) -> Dict[str, Any]:
    acc = account or "global"
    p = recall_data(acc, "__personality__")
    if p:
        return p.get("value") if isinstance(p, dict) and "value" in p else p
    return DEFAULT_PERSONALITY

def set_personality(account: Optional[str], personality: Dict[str, Any]) -> Dict[str, Any]:
    acc = account or "global"
    merged = {**DEFAULT_PERSONALITY, **personality}
    remember_data(acc, "__personality__", merged)
    return merged

# ----------------- Agent -----------------
class RockyAgent:
    FACT_PATTERNS = {
        "name": re.compile(r"\bmy name is ([A-Z][a-zA-Z\-']+)", re.I),
        "birthday": re.compile(r"\b(my birthday is|born on|my bday is)\s*(on\s*)?([A-Za-z0-9 ,]+)", re.I),
        "email": re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"),
        "phone": re.compile(r"(\+?\d[\d\-\s]{6,}\d)"),
        "like": re.compile(r"\bi like ([a-zA-Z0-9 ]{2,50})", re.I),
        "dislike": re.compile(r"\bi (?:don't|do not) like ([a-zA-Z0-9 ]{2,50})", re.I),
    }

    def __init__(self, account: str, thread_id: Optional[str] = None):
        self.account = account
        self.thread_id = thread_id or f"{account}::default"
        self.personality = get_personality(account)


    def _log_user(self, text: str):
        log_thread(self.account, self.thread_id, [{"role": "user", "content": text, "timestamp": now_iso()}])

    def _log_assistant(self, text: str):
        log_thread(self.account, self.thread_id, [{"role": "assistant", "content": text, "timestamp": now_iso()}])

    def _extract_facts(self, text: str) -> Dict[str, str]:
        facts: Dict[str, str] = {}
        for k, pat in self.FACT_PATTERNS.items():
            m = pat.search(text)
            if m:
                if k == "name":
                    facts["name"] = m.group(1).strip()
                elif k == "birthday":
                    facts["birthday"] = (m.group(3) or "").strip()
                else:
                    facts[k] = m.group(1).strip()
        return facts

    def _save_facts(self, facts: Dict[str, str]):
        for k, v in facts.items():
            remember_data(self.account, k, v)

    def _check_repetition(self, reply: str) -> bool:
        msgs = fetch_thread_messages(self.account, self.thread_id)
        return any(m["role"] == "assistant" and m["content"] == reply for m in msgs)

    def _save_failure(self, query: str, attempt: str):
        remember_data(self.account, f"failure::{hash(query+attempt)}", {"query": query, "attempt": attempt, "status": "failed", "time": now_iso()})

    def reply(self, user_message: str, auto_save: bool = True, use_llm: bool = False) -> str:
        self._log_user(user_message)
        facts = self._extract_facts(user_message)
        if auto_save and facts:
            self._save_facts(facts)
        res = scan_and_respond(self.account, self.thread_id, user_message, max_context=10, use_llm=False)
        reply = res.get("suggested_reply") or res.get("reply") or ""
        if self._check_repetition(reply):
            reply = f"⚠️ I already suggested that earlier. Let me rethink... {self.personality['signature']}"
            self._save_failure(user_message, reply)
            if use_llm and HAS_OPENAI:
                try:
                    prompt = f"User asked:\n{user_message}\nMy previous attempts failed. Suggest a new approach."
                    resp = openai.ChatCompletion.create(model="gpt-4o-mini", messages=[{"role": "system", "content": "You are ALADDIN, a problem-solver."}, {"role": "user", "content": prompt}], max_tokens=400, temperature=0.7)
                    reply = resp["choices"][0]["message"]["content"].strip()
                except Exception as e:
                    reply += f"\n(LLM escalation failed: {e})"
        if self.personality.get("signature"):
            reply = f"{reply} {self.personality['signature']}"
        if self.personality.get("style") == "cofounder-high-energy":
            reply = reply.upper()
        self._log_assistant(reply)
        return reply

# ----------------- FastAPI -----------------
if HAS_FASTAPI:
    app = FastAPI(title="Rocky Soulmode API", version="v∞")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    class MemReq(BaseModel):
        account: Optional[str]
        key: str
        value: Any
        tags: Optional[List[str]] = Field(default_factory=list)

    class ThreadMsg(BaseModel):
        role: str
        content: str
        timestamp: Optional[str] = None

    class ThreadReq(BaseModel):
        account: Optional[str]
        thread_id: str
        messages: List[ThreadMsg]

    class ScanReq(BaseModel):
        account: Optional[str]
        thread_id: Optional[str] = None
        query: Optional[str] = None
        max_context_messages: Optional[int] = 10
        use_llm: Optional[bool] = False

    @app.post("/remember")
    def api_remember(req: MemReq):
        return remember_data(req.account, req.key, req.value, req.tags)

    @app.get("/recall/{account}/{key}")
    def api_recall(account: str, key: str):
        doc = recall_data(account, key)
        if not doc:
            raise HTTPException(status_code=404, detail="not found")
        return doc

    @app.post("/login/{account}")
    def api_login(account: str):
        if not recall_data(account, "__personality__"):
            set_personality(account, DEFAULT_PERSONALITY)
        remember_data(account, "__session__", {"status": "online", "last_seen": now_iso()})
        return {"status": "logged_in"}

    @app.post("/logout/{account}")
    def api_logout(account: str):
        remember_data(account, "__session__", {"status": "offline", "last_seen": now_iso()})
        msgs = fetch_thread_messages(account, f"{account}::default")
        if msgs:
            remember_data(account, f"archive::{now_iso()}", {"thread": msgs})
        return {"status": "logged_out"}
     
     
    @app.delete("/forget/{account}/{key}")
    def api_forget(account: str, key: str):
        ok = forget_data(account, key)
        return {"status": "forgotten" if ok else "not_found"}

    @app.get("/export/{account}")
    def api_export(account: str):
        return export_all(account)

    @app.post("/log_thread")
    def api_log_thread(body: ThreadReq):
        msgs = [{"role": m.role, "content": m.content, "timestamp": m.timestamp or now_iso()} for m in body.messages]
        return log_thread(body.account, body.thread_id, msgs)

    @app.post("/scan_and_respond")
    def api_scan(body: ScanReq):
        return scan_and_respond(body.account, body.thread_id, body.query, body.max_context_messages or 10, body.use_llm or False)

    @app.post("/agent/{account}")
    def api_agent(account: str, payload: Dict[str, Any]):
        message = payload.get("message", "")
        agent = RockyAgent(account)
        reply = agent.reply(message)
        return {"reply": reply}

    @app.get("/chat_ui.html")
    def serve_ui():
        path = os.path.join(os.path.dirname(__file__), "chat_ui.html")
        if os.path.exists(path):
            return FileResponse(path)
        return {"detail": "chat_ui.html not found"}

# ----------------- Demo -----------------
def run_demo():
    print("Running Rocky Soulmode local demo (no network).")
    acc = "demo_user"
    agent = RockyAgent(acc)
    print("User ->: My name is Alex.")
    print("Agent ->:", agent.reply("My name is Alex."))
    print("User ->: What's my name?")
    print("Agent ->:", agent.reply("What's my name?"))
    print("Exported memories:\n", json.dumps(export_all(acc), indent=2))

# ----------------- Tests -----------------
class CoreTests(unittest.TestCase):
    def test_memory_cycle(self):
        remember_data("tacc", "k1", "v1")
        doc = recall_data("tacc", "k1")
        self.assertIsNotNone(doc)
        self.assertEqual(doc.get("value"), "v1")

    def test_forget(self):
        remember_data("tacc2", "ktemp", "val")
        forget_data("tacc2", "ktemp")
        self.assertIsNone(recall_data("tacc2", "ktemp"))

    def test_threads(self):
        log_thread("tacc3", "tid1", [{"role": "user", "content": "hello"}])
        msgs = fetch_thread_messages("tacc3", "tid1")
        self.assertTrue(isinstance(msgs, list))

    def test_agent_fact_save(self):
        a = RockyAgent("tacc4", "tidx")
        a.reply("My name is Sam")
        doc = recall_data("tacc4", "name")
        self.assertIsNotNone(doc)
        self.assertEqual(doc.get("value"), "Sam")

# Entrypoint
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ("test", "--test"):
        unittest.main(argv=[sys.argv[0]])
    elif HAS_FASTAPI:
        allow_server = os.getenv("ROCKY_ALLOW_SERVER", "0")
        if allow_server == "1":
            try:
                import uvicorn
                port = int(os.getenv("PORT", "8000"))
                uvicorn.run("rocky_soulmode_api:app", host="127.0.0.1", port=port)
            except Exception as e:
                logger.error(f"Failed to start uvicorn: {e}")
        else:
            run_demo()
    else:
        run_demo()

# ----------------- Worker -----------------
import threading, time, requests, random
from datetime import timedelta

def rocky_worker_loop():
    interval = int(os.getenv("ROCKY_WORKER_INTERVAL", "300"))
    auto_reply = os.getenv("ROCKY_AUTO_REPLY", "0") == "1"
    use_llm = os.getenv("ROCKY_USE_LLM", "0") == "1"
    webhook = os.getenv("ROCKY_NOTIFY_WEBHOOK")
    active_accounts_env = os.getenv("ROCKY_ACTIVE_ACCOUNTS")
    active_accounts = [a.strip() for a in active_accounts_env.split(",")] if active_accounts_env else None
    sync_personality = os.getenv("ROCKY_SYNC_PERSONALITY", "0") == "1"
    self_reflect = os.getenv("ROCKY_SELF_REFLECT", "0") == "1"
    ttl_days = os.getenv("ROCKY_MEMORY_TTL")
    ttl = None
    if ttl_days:
        try:
            ttl = int(ttl_days.replace("d", ""))
        except Exception:
            logger.warning(f"[WORKER] Invalid TTL format: {ttl_days}")

    def get_accounts():
        if firestore_client:
            try:
                accounts = set()
                for doc in firestore_client.collection("memories").stream():
                    d = doc.to_dict()
                    accounts.add(d.get("account") or "global")
                return list(accounts) or ["global"]
            except Exception as e:
                logger.warning(f"[WORKER] Firestore account fetch failed: {e}")
        return list(_local_memory.keys()) or ["global"]

    def cleanup_memories(acc: str):
        if not ttl:
            return
        cutoff = datetime.utcnow() - timedelta(days=ttl)
        if firestore_client:
            try:
                for snap in firestore_client.collection("memories").where("account", "==", acc).stream():
                    d = snap.to_dict()
                    ts = d.get("timestamp")
                    if not ts:
                        continue
                    try:
                        tdt = datetime.fromisoformat(ts)
                    except Exception:
                        if hasattr(ts, "tzinfo"):
                            tdt = ts
                        else:
                            continue
                    if tdt < cutoff:
                        d["archived"] = True
                        firestore_client.collection("memories").document(snap.id).set(d)
                        logger.info(f"[WORKER] Archived {acc}:{d.get('key')} (TTL expired)")
            except Exception as e:
                logger.warning(f"[WORKER] TTL Firestore cleanup failed: {e}")
        for key, doc in list(_local_memory.get(acc, {}).items()):
            try:
                ts = datetime.fromisoformat(doc.get("timestamp", now_iso()))
                if ts < cutoff:
                    doc["archived"] = True
                    _local_memory[acc][key] = doc
            except Exception:
                pass

    while True:
        try:
            accounts = get_accounts()
            if active_accounts:
                accounts = [a for a in accounts if a in active_accounts]
            for acc in accounts:
                if sync_personality and not recall_data(acc, "__personality__"):
                    set_personality(acc, DEFAULT_PERSONALITY)
                if ttl:
                    cleanup_memories(acc)
                payload = {"account": acc, "thread_id": None, "query": None, "max_context_messages": 10, "use_llm": use_llm}
                r = requests.post("http://127.0.0.1:8000/scan_and_respond", json=payload, timeout=15)
                data = r.json()
                reply = data.get("suggested_reply", "")
                if auto_reply and reply:
                    requests.post(f"http://127.0.0.1:8000/agent/{acc}", json={"message": reply})
                if webhook and reply:
                    try:
                        requests.post(webhook, json={"text": f"[{acc}] {reply}"})
                    except Exception as we:
                        logger.warning(f"[WORKER] webhook failed: {we}")
                if self_reflect and random.random() < 0.2:
                    msgs = fetch_thread_messages(acc, f"{acc}::default")
                    past_replies = [m["content"] for m in msgs if m["role"] == "assistant"]
                    if past_replies:
                        reflection = f"REFLECTION: Reviewed {len(past_replies)} replies. Improvement: be concise and proactive."
                        remember_data(acc, f"self_reflection::{now_iso()}", reflection)
        except Exception as e:
            logger.error(f"[WORKER] loop error: {e}")
        time.sleep(interval)

if os.getenv("ROCKY_AUTONOMOUS", "0") == "1":
    t = threading.Thread(target=rocky_worker_loop, daemon=True)
    t.start()
    logger.info("🚀 Rocky Autonomous Worker started")

def start_worker_if_needed():
    if os.getenv("ROCKY_AUTONOMOUS", "0") == "1":
        t = threading.Thread(target=rocky_worker_loop, daemon=True)
        t.start()
        logger.info("🚀 Rocky Autonomous Worker (extended) started")

# Ensure worker starts even if launched with uvicorn CLI
start_worker_if_needed()

