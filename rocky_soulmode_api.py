# rocky_soulmode_api.py
import os
import re
import sys
import json
import logging
import unittest
import threading
import time
import requests
import random
import traceback

def log_firestore_error(action: str, account: str, key: str, error: Exception):
    logger.error(
        f"\n🚨 FIRESTORE ERROR during {action}\n"
        f"   account = {account}\n"
        f"   key     = {key}\n"
        f"   type    = {type(error).__name__}\n"
        f"   details = {str(error)}\n"
        f"   trace   = {traceback.format_exc()}"
    )

from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

"""
=========================================
🚀 Rocky Soulmode Configuration (ENV Vars)
=========================================
(omitted here for brevity — same as your original docstring)
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

def _safe_firestore_key(key: str) -> str:
    """
    Firestore does not allow keys starting with '__'.
    Map them to 'reserved_<name>'.
    """
    if key.startswith("__"):
        return f"reserved_{key.strip('_')}"
    return key

# ----------------- Storage operations -----------------
def _ensure_account(account: Optional[str]):
    acc = account or "global"
    if acc not in _local_memory:
        _local_memory[acc] = {}
    if acc not in _local_threads:
        _local_threads[acc] = {}
    return acc

def remember_data(account: Optional[str], key: str, value: Any, tags: Optional[List[str]] = None) -> Dict[str, Any]:
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
            safe_key = _safe_firestore_key(key)
            firestore_client.collection("memories").document(acc).collection("items").document(safe_key).set(doc)
            logger.info(f"[FIRESTORE] Saved memory {acc}:{safe_key}")
        except Exception as e:
               log_firestore_error("save", acc, key, e)
    _local_memory[acc][key] = doc
    return doc

def recall_data(account: Optional[str], key: str) -> Optional[Dict[str, Any]]:
    acc = account or "global"
    if firestore_client:
        try:
            safe_key = _safe_firestore_key(key)
            snap = firestore_client.collection("memories").document(acc).collection("items").document(safe_key).get()
            if snap.exists:
                return snap.to_dict()
        except Exception as e:
             log_firestore_error("recall", acc, key, e)
    return _local_memory.get(acc, {}).get(key)

def forget_data(account: Optional[str], key: str) -> bool:
    acc = account or "global"
    removed = False
    if firestore_client:
        try:
            safe_key = _safe_firestore_key(key)
            firestore_client.collection("memories").document(acc).collection("items").document(safe_key).delete()
            removed = True
        except Exception as e:
            log_firestore_error("delete", acc, key, e)
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
             log_firestore_error("thread_log", acc, thread_id, e)
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
                 log_firestore_error("scan", acc, "threads", e)
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
    "tone": "professional-friendly",        # warm + respectful
    "style": "proactive-solution-oriented", # anticipates user needs
    "signature": "⚡💎",                     # strong but not too flashy
    "responsibility": "high",               # always follow through
    "consistency": "stable",                # same behavior every time
    "adaptability": "learning",             # grows with new info
    "thinking": "strategic-creative",       # balance logic + creativity
    "focus": "customer-success",            # priority: user outcomes
}

HIGHEST_PERSONALITY = {
    "tone": "assertive-proactive",
    "style": "executive-delegate",
    "signature": "🚀🔥",
    "include_oob": True,
    "thinking": "decisive",
    "responsibility": "max",
    "consistency": "strict",
    "proactivity": "always",
    "conciseness": "high",
}

def get_personality(account: Optional[str]) -> Dict[str, Any]:
    acc = account or "global"
    p = recall_data(acc, "personality")
    if p:
        return p.get("value") if isinstance(p, dict) and "value" in p else p
    return DEFAULT_PERSONALITY

def set_personality(account: Optional[str], personality: Dict[str, Any]) -> Dict[str, Any]:
    acc = account or "global"
    # ✅ Always merge so nothing gets lost
    merged = {**DEFAULT_PERSONALITY, **personality}
    remember_data(acc, "personality", merged)
    return merged

def elevate_personality(account: Optional[str], level: str = "highest") -> Dict[str, Any]:
    """
    Apply a preset personality immediately and persist it.
    level: 'highest' | 'default' (extend with more presets later)
    """
    if level == "highest":
        new = {**DEFAULT_PERSONALITY, **HIGHEST_PERSONALITY}
    elif level == "default":
        new = DEFAULT_PERSONALITY.copy()
    else:
        new = DEFAULT_PERSONALITY.copy()  # fallback

    set_personality(account, new)
    return new

class RockyAgent:
    # ----------------- Fact Patterns -----------------
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

    # ----------------- Helpers -----------------
    def _log_user(self, text: str):
        log_thread(self.account, self.thread_id,
                   [{"role": "user", "content": text, "timestamp": now_iso()}])

    def _log_assistant(self, text: str):
        log_thread(self.account, self.thread_id,
                   [{"role": "assistant", "content": text, "timestamp": now_iso()}])

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
        remember_data(
            self.account,
            f"failure::{hash(query+attempt)}",
            {"query": query, "attempt": attempt,
             "status": "failed", "time": now_iso()}
        )

    # ----------------- Core Reply -----------------
    def reply(self, user_message: str, auto_save: bool = True, use_llm: bool = False) -> str:
        self._log_user(user_message)
        msg = (user_message or "").strip()
        lm = msg.lower()

        # 🔑 Aliases
        aliases = {
            "addm": "addmem",
            "fdel": "fmem",
            "lmem": "listmem",
            "brop": "bropersonality",
            "brops": "bropersonalitystatus",
            "bropr": "bropersonalityreset",
            "bropd": "bropersonalitydefault",
            "brofix": "bronotcorrect",
            "rpt": "reports",
        }
        for short, full in aliases.items():
            if lm.startswith(short):
                lm = lm.replace(short, full, 1)
                msg = msg.replace(short, full, 1)
                break

        # ---------------- Personality Commands ----------------
        if lm.startswith("bro personality"):
            if "status" in lm:
                p = get_personality(self.account)
                reply = "🎭 Current personality:\n" + "\n".join([f"{k}: {v}" for k, v in p.items()])
                self.personality = p
                self._log_assistant(reply)
    return reply 
            if "reset" in lm or "default" in lm:
                new = elevate_personality(self.account, level="default")
                self.personality = new
                reply = "♻️ Personality reset to DEFAULT."
                remember_data(self.account, f"personality_log::{now_iso()}",
                              {"action": "reset", "traits": new})
                self._log_assistant(reply)
                return reply

            # default → highest
            new = elevate_personality(self.account, level="highest")
            self.personality = new
            reply = "⚡ Personality elevated to HIGHEST (proactive/executive). I will be more assertive, concise and action-focused."
            remember_data(self.account, f"personality_log::{now_iso()}",
                          {"action": "highest", "traits": new})
            self._log_assistant(reply)
            return reply

        if lm.startswith("bro not correct") or ("bro" in lm and "not correct" in lm):
            new = elevate_personality(self.account, level="highest")
            self.personality = new
            reply = "⚠️ Understood — escalating personality to HIGHEST to correct course."
            remember_data(self.account, f"personality_log::{now_iso()}",
                          {"action": "escalate", "traits": new})
            self._log_assistant(reply)
            return reply
# ---------------- Save Last Assistant Reply ----------------
        if lm.startswith(("addlast ", "alast ", "savelast ", "slast ", "storelast ", "stlast ")):
            try:
                _, key = msg.split(" ", 1)
                key = key.strip()

                msgs = fetch_thread_messages(self.account, self.thread_id)
                last_assistant = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)

                if not last_assistant:
                    reply = "⚠️ No assistant reply found to save."
                else:
                    value = last_assistant["content"]

                    remember_data(self.account, key, value)
                    ts_key = f"{key}::v::{datetime.utcnow().isoformat()}"
                    remember_data(self.account, ts_key, value)

                    check = recall_data(self.account, key)
                    if check and check.get("value") == value:
                        reply = f"✅ Bro last reply saved under '{key}' (verified)\n🕒 Snapshot: {ts_key}"
                    else:
                        reply = f"⚠️ Tried saving last reply as '{key}', but verification failed"
            except Exception as e:
                reply = f"⚠️ Format: addlast key (error: {e})"

            self._log_assistant(reply)
               return reply
#---------------- Manual Memory Commands ----------------
        if lm.startswith("addmem "):
            try:
                _, pair = msg.split(" ", 1)
                key, value = pair.split(":", 1)
                key, value = key.strip(), value.strip()

                chunks = [value[i:i+1000] for i in range(0, len(value), 1000)]
                remember_data(self.account, key, {"value": value, "chunks": len(chunks)})
                for idx, chunk in enumerate(chunks):
                    remember_data(self.account, f"{key}::chunk::{idx}", chunk)

                reply = f"✅ Memory saved under '{key}' ({len(chunks)} chunk(s))."
            except Exception as e:
                reply = f"⚠️ Use: addmem key: value  (error: {e})"
            self._log_assistant(reply)
            return reply

        if lm.startswith("fmem "):
            try:
                _, key = msg.split(" ", 1)
                key = key.strip()
                ok = forget_data(self.account, key)

                idx = 0
                while recall_data(self.account, f"{key}::chunk::{idx}"):
                    forget_data(self.account, f"{key}::chunk::{idx}")
                    idx += 1

                reply = f"🗑️ Memory removed: '{key}'" if ok else f"⚠️ No memory found for '{key}'"
            except Exception as e:
                reply = f"⚠️ Use: fmem key  (error: {e})"
            self._log_assistant(reply)
            return reply

        if lm.startswith("getmem "):
            try:
                _, key = msg.split(" ", 1)
                key = key.strip()
                doc = recall_data(self.account, key)
                if not doc:
                    reply = f"⚠️ No memory for '{key}'"
                else:
                    chunks = []
                    idx = 0
                    while True:
                        c = recall_data(self.account, f"{key}::chunk::{idx}")
                        if not c:
                            break
                        chunks.append(c.get("value") if isinstance(c, dict) and "value" in c else c)
                        idx += 1
                    full_value = doc.get("value") if isinstance(doc, dict) and "value" in doc else doc
                    if chunks:
                        full_value = "".join(chunks)
                    preview = full_value if len(full_value) <= 2000 else full_value[:2000] + "..."
                    reply = f"📦 {key}: {preview}"
            except Exception as e:
                reply = f"⚠️ Use: getmem key  (error: {e})"
            self._log_assistant(reply)
            return reply

        if lm.strip() == "listmem":
            mems = export_all(self.account).get("memories", {})
            keys = [k.split("::")[-1] for k in mems.keys() if "::chunk::" not in k]
            if not keys:
                reply = "📭 No memories found."
            else:
                reply = "🧠 Memories:\n- " + "\n- ".join(keys[:100])
                if len(keys) > 100:
                    reply += f"\n...and {len(keys)-100} more"
            self._log_assistant(reply)
            return reply

        if lm.strip() == "reports":
            mems = export_all(self.account).get("memories", {})
            reports = [v for k, v in mems.items() if k.startswith(f"{self.account}::report::") or k.startswith("report::")]
            if not reports:
                reply = "⚠️ No reports found."
            else:
                latest = reports[-1]
                reply = f"📊 Latest report:\nTime: {latest.get('time')}\nMessages: {latest.get('total_msgs')}\nReflections: {latest.get('total_reflections')}"
            self._log_assistant(reply)
            return reply
# ---------------- Normal flow ----------------
    facts = self._extract_facts(user_message)
    if auto_save and facts:
        self._save_facts(facts)

    res = scan_and_respond(self.account, self.thread_id, user_message, max_context=10, use_llm=use_llm)
    reply = res.get("suggested_reply") or res.get("reply") or ""

    if self._check_repetition(reply):
        reply = f"⚠️ I already suggested that earlier. Let me rethink... {self.personality.get('signature', '')}"
        self._save_failure(user_message, reply)
        if use_llm and HAS_OPENAI:
            try:
               prompt = f"User asked:\n{user_message}\nMy previous attempts failed. Suggest a new approach."
                    resp = openai.ChatCompletion.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "You are ALADDIN, a problem-solver."},
                            {"role": "user", "content": prompt},
                        ],
                        max_tokens=400,
                        temperature=0.7
                    )
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

    # ✅ Health check root route
    @app.get("/")
    def root():
        return {"status": "ok", "service": "rocky-soulmode"}

    # ✅ Keepalive-friendly route for external pings
    @app.get("/test/selfcheck")
    def selfcheck():
        return {"ok": True, "time": now_iso()}

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
        if not recall_data(account, "personality"):
            set_personality(account, DEFAULT_PERSONALITY)
        remember_data(account, "session", {"status": "online", "last_seen": now_iso()})
        return {"status": "logged_in"}

    @app.post("/logout/{account}")
    def api_logout(account: str):
        remember_data(account, "session", {"status": "offline", "last_seen": now_iso()})
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

# ----------------- Keepalive Loop -----------------
import asyncio, httpx, pytz
from datetime import timedelta

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
KEEPALIVE_INTERVAL_MS = int(os.getenv("KEEPALIVE_INTERVAL_MS") or 600000)  # default 10 min

async def keepalive_loop():
    if not RENDER_EXTERNAL_URL:
        logger.warning("⚠️ No RENDER_EXTERNAL_URL set, skipping keepalive")
        return

    ist = pytz.timezone("Asia/Kolkata")

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{RENDER_EXTERNAL_URL}/test/selfcheck")
            next_ping = datetime.now(ist) + timedelta(milliseconds=KEEPALIVE_INTERVAL_MS)
            logger.info(f"🔄 Keepalive ping {r.status_code} | next @ {next_ping.strftime('%I:%M:%S %p')}")
        except Exception as e:
            logger.error(f"⚠️ Keepalive ping failed: {e}")
        await asyncio.sleep(KEEPALIVE_INTERVAL_MS / 1000.0)

def start_keepalive():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(keepalive_loop())


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
                # bind to 0.0.0.0 so containers & external pings can reach it
                uvicorn.run("rocky_soulmode_api:app", host="0.0.0.0", port=port)
            except Exception as e:
                logger.error(f"Failed to start uvicorn: {e}")
        else:
            run_demo()
    else:
        run_demo()

# ----------------- Worker -----------------
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

    # small warmup delay
    time.sleep(5)

    while True:
        try:
            accounts = get_accounts()
            if active_accounts:
                accounts = [a for a in accounts if a in active_accounts]
            for acc in accounts:
                if sync_personality and not recall_data(acc, "personality"):
                    set_personality(acc, DEFAULT_PERSONALITY)
                if ttl:
                    cleanup_memories(acc)
                payload = {"account": acc, "thread_id": None, "query": None, "max_context_messages": 10, "use_llm": use_llm}
                try:
                    r = requests.post("http://127.0.0.1:8000/scan_and_respond", json=payload, timeout=15)
                    data = r.json() if r.status_code == 200 else {}
                except Exception as rexc:
                    logger.warning(f"[WORKER] scan_and_respond request failed for {acc}: {rexc}")
                    data = {}
                reply = data.get("suggested_reply", "")
                if auto_reply and reply:
                    try:
                        requests.post(f"http://127.0.0.1:8000/agent/{acc}", json={"message": reply}, timeout=10)
                    except Exception as e:
                        logger.warning(f"[WORKER] auto_reply failed for {acc}: {e}")
                if webhook and reply:
                    try:
                        requests.post(webhook, json={"text": f"[{acc}] {reply}"}, timeout=10)
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
# 📊 Save daily/hourly reports        
def save_report(acc: str, period: str):
    msgs = fetch_thread_messages(acc, f"{acc}::default")
    reflections = [m for m in msgs if "REFLECTION" in m.get("content", "")]
    report = {
        "time": now_iso(),
        "total_msgs": len(msgs),
        "total_reflections": len(reflections),
        "highlights": reflections[-3:],  # last few
    }
    remember_data(acc, f"report::{period}::{now_iso()}", report)
    logger.info(f"[REPORT] Saved {period} report for {acc}")

def start_worker_if_needed():
    if os.getenv("ROCKY_AUTONOMOUS", "0") == "1":
        t = threading.Thread(target=rocky_worker_loop, daemon=True)
        t.start()
        logger.info("🚀 Rocky Autonomous Worker started")

# Ensure worker starts when module imported (e.g. uvicorn)
start_worker_if_needed()
# Start keepalive loop in background thread
if RENDER_EXTERNAL_URL:
    threading.Thread(target=start_keepalive, daemon=True).start()
    logger.info("🚀 Keepalive loop started")
else:
    logger.warning("⚠️ Keepalive not started because RENDER_EXTERNAL_URL is missing")








