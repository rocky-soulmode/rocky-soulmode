# rocky_soulmode_api.py (REVISED)
import logging
import os
import re
import sys
import json
import unittest
import threading
import time
import requests
import random
import traceback
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
import redis, json

# Redis connection (adjust host/port if needed)
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# google exceptions + retry (guarded imports)
try:
    from google.api_core.exceptions import ResourceExhausted
    from google.api_core.retry import Retry
except Exception:
    ResourceExhausted = Exception
    # define a no-op Retry placeholder if import fails; Firestore will be disabled anyway
    class Retry:
        def __init__(self, *a, **k):
            pass

# ----------------- Basic helpers -----------------
def safe_reply(reply, fallback="⚠️ I didn’t understand that."):
    """
    Ensures 'reply' is always safe to return.
    Logs a warning if fallback is used.
    """
    if reply is None:
        logging.getLogger("rocky_soulmode").warning("⚠️ safe_reply triggered fallback (reply was None)")
        return fallback
    return reply

# ----------------- Firestore circuit variables -----------------
_FIRESTORE_QUOTA_EVENTS = 0
_FIRESTORE_QUOTA_BACKOFF_UNTIL = 0.0
_FIRESTORE_QUOTA_THRESHOLD = 3    # events before cooldown
_FIRESTORE_QUOTA_COOLDOWN = 300   # seconds to skip Firestore after threshold reached

def _note_firestore_quota_event():
    global _FIRESTORE_QUOTA_EVENTS, _FIRESTORE_QUOTA_BACKOFF_UNTIL
    now = time.time()
    _FIRESTORE_QUOTA_EVENTS += 1
    if _FIRESTORE_QUOTA_EVENTS >= _FIRESTORE_QUOTA_THRESHOLD:
        _FIRESTORE_QUOTA_BACKOFF_UNTIL = now + _FIRESTORE_QUOTA_COOLDOWN

def _can_use_firestore():
    try:
        return (firestore_client is not None) and (time.time() > _FIRESTORE_QUOTA_BACKOFF_UNTIL)
    except Exception:
        return False

# ----------------- Redis (Upstash REST) -----------------
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "https://bright-aardvark-8251.upstash.io")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

def redis_rest_cmd(command: str, *args):
    """Send a Redis command to Upstash via REST API (best-effort)."""
    lg = logging.getLogger("rocky_soulmode")
    try:
        if not UPSTASH_TOKEN or not UPSTASH_URL:
            return None
        url = f"{UPSTASH_URL}/{command}"
        headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"} if UPSTASH_TOKEN else {}
        payload = [str(a) for a in args]
        res = requests.post(url, headers=headers, json=payload, timeout=5)
        res.raise_for_status()
        data = res.json()
        return data.get("result")
    except Exception as e:
        lg.debug("[Redis REST] %s failed: %s", command, e)
        return None

def cache_set(key: str, value: Any, ttl: int = 60):
    lg = logging.getLogger("rocky_soulmode")
    try:
        # prefer python redis client if configured, otherwise Upstash REST
        if redis_client:
            try:
                redis_client.setex(key, ttl, json.dumps(value))
                return
            except Exception:
                pass
        redis_rest_cmd("setex", key, ttl, json.dumps(value))
    except Exception as e:
        lg.warning(f"[Cache] Failed set {key}: {e}")

def cache_get(key: str) -> Optional[Any]:
    lg = logging.getLogger("rocky_soulmode")
    try:
        if redis_client:
            try:
                val = redis_client.get(key)
                if val:
                    try:
                        return json.loads(val)
                    except Exception:
                        return val
            except Exception:
                pass
        val = redis_rest_cmd("get", key)
        if val:
            try:
                return json.loads(val)
            except Exception:
                return val
    except Exception as e:
        lg.warning(f"[Cache] Failed get {key}: {e}")
    return None

# ----------------- Optional / third-party flags -----------------
HAS_FASTAPI = False
HAS_PYDANTIC = False
HAS_OPENAI = False
firestore_client = None

# Try to import FastAPI & Pydantic (optional)
try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from pydantic import BaseModel, Field
    HAS_FASTAPI = True
    HAS_PYDANTIC = True
except Exception:
    HAS_FASTAPI = False

# Try to import Firestore safely
try:
    from google.cloud import firestore  # type: ignore
    from google.oauth2 import service_account  # type: ignore
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.exists(cred_path):
        creds = service_account.Credentials.from_service_account_file(
            cred_path,
            scopes=["https://www.googleapis.com/auth/datastore"]
        )
        firestore_client = firestore.Client(credentials=creds, project=creds.project_id)
        print(f"🔥 Connected to Firestore project: {creds.project_id}")
    else:
        firestore_client = None
        print("⚠️ Firestore not available: GOOGLE_APPLICATION_CREDENTIALS not set or file missing")
except Exception as e:
    firestore_client = None
    print("⚠️ Firestore import failed or not configured, falling back to local memory:", e)

# Try to import OpenAI client safely
try:
    import openai  # type: ignore
    if os.getenv("OPENAI_API_KEY"):
        openai.api_key = os.getenv("OPENAI_API_KEY")
        HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rocky_soulmode")

# ----------------- Local fallback & compatibility helpers -----------------
LOCAL_STORE: Dict[str, Dict[str, Any]] = {}
_local_memory: Dict[str, Dict[str, Any]] = {}   # account -> key -> doc
_local_threads: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}  # account -> thread_id -> messages

# Optionally set a python `redis` client into `redis_client` variable at runtime.
redis_client = None  # by default we use Upstash REST (redis_rest_cmd)

def safe_str(s: Optional[str]) -> str:
    if s is None:
        return ""
    return str(s).strip().replace("/", "_").replace(" ", "_")

def invalidate_thread_cache_for_account(acc: str):
    """Best-effort cache invalidation for thread scan results."""
    try:
        if redis_client:
            pattern = f"threads:{acc}:*"
            for key in redis_client.scan_iter(match=pattern, count=1000):
                try:
                    redis_client.delete(key)
                except Exception:
                    pass
        else:
            # Upstash REST scan not implemented reliably here — skip silently
            pass
    except Exception:
        pass

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
    if key.startswith("__"):
        return f"reserved_{key.strip('_')}"
    return key

def log_firestore_error(action: str, account: str, key: str, error: Exception):
    lg = logging.getLogger("rocky_soulmode")
    lg.error(
        f"\n🚨 FIRESTORE ERROR during {action}\n"
        f"   account = {account}\n"
        f"   key     = {key}\n"
        f"   type    = {type(error).__name__}\n"
        f"   details = {str(error)}\n"
        f"   trace   = {traceback.format_exc()}"
    )

# ----------------- Storage operations -----------------
def _ensure_account(account: Optional[str]):
    acc = account or "global"
    if acc not in _local_memory:
        _local_memory[acc] = {}
    if acc not in _local_threads:
        _local_threads[acc] = {}
    return acc

def remember_data(acc: str, key: str, value):
    acc = acc or "default"
    safe_key = safe_str(key)
    data = {"value": value, "ts": datetime.utcnow().isoformat()}

    # Redis write (either python redis client or Upstash REST)
    try:
        if redis_client:
            try:
                redis_client.hset(f"mem:{acc}", safe_key, json.dumps(data))
            except Exception:
                # fallback to setex / hset via Upstash REST if python client fails
                redis_rest_cmd("hset", f"mem:{acc}", safe_key, json.dumps(data))
        else:
            redis_rest_cmd("hset", f"mem:{acc}", safe_key, json.dumps(data))
    except Exception as e:
        logger.warning("Redis write failed for %s:%s: %s", acc, safe_key, e)

    # Firestore best-effort (short deadline)
    if _can_use_firestore():
        try:
            no_retry = Retry(deadline=5.0, maximum=1)
            firestore_client.collection("memories").document(acc).collection("items").document(safe_key).set(data, retry=no_retry)
        except Exception as e:
            logger.warning("Firestore write skipped for %s:%s -> %s", acc, safe_key, e)
            if isinstance(e, ResourceExhausted) or "Quota exceeded" in str(e):
                _note_firestore_quota_event()

    # Local fallback
    LOCAL_STORE.setdefault(acc, {})[safe_key] = data

    # Invalidate caches for account (best-effort)
    try:
        invalidate_thread_cache_for_account(acc)
    except Exception:
        pass

    return data

def recall_data(acc: str, key: str):
    acc = acc or "default"
    safe_key = safe_str(key)

    # Try Redis first (python client or Upstash REST)
    try:
        if redis_client:
            try:
                val = redis_client.hget(f"mem:{acc}", safe_key)
            except Exception:
                val = None
        else:
            val = redis_rest_cmd("hget", f"mem:{acc}", safe_key)
        if val:
            try:
                return json.loads(val)
            except Exception:
                return {"value": val}
    except Exception as e:
        logger.warning("Redis read failed (%s:%s): %s", acc, safe_key, e)

    # Firestore quick/no-retry get (best-effort)
    if _can_use_firestore():
        try:
            no_retry = Retry(deadline=5.0, maximum=1)
            doc = firestore_client.collection("memories").document(acc).collection("items").document(safe_key).get(retry=no_retry)
            if doc and getattr(doc, "exists", False):
                data = doc.to_dict()
                # cache back into Redis
                try:
                    if redis_client:
                        redis_client.hset(f"mem:{acc}", safe_key, json.dumps(data))
                    else:
                        redis_rest_cmd("hset", f"mem:{acc}", safe_key, json.dumps(data))
                except Exception:
                    pass
                return data
        except Exception as e:
            logger.warning("Firestore recall failed for %s:%s -> %s", acc, safe_key, e)
            if isinstance(e, ResourceExhausted) or "Quota exceeded" in str(e):
                _note_firestore_quota_event()

    # Local fallback
    return LOCAL_STORE.get(acc, {}).get(safe_key)

def forget_data(acc: str, key: str):
    acc = acc or "default"
    safe_key = safe_str(key)
    try:
        if redis_client:
            try:
                redis_client.hdel(f"mem:{acc}", safe_key)
            except Exception:
                redis_rest_cmd("hdel", f"mem:{acc}", safe_key)
        else:
            redis_rest_cmd("hdel", f"mem:{acc}", safe_key)
    except Exception as e:
        logger.warning("Redis delete failed for %s:%s -> %s", acc, safe_key, e)

    if _can_use_firestore():
        try:
            no_retry = Retry(deadline=5.0, maximum=1)
            firestore_client.collection("memories").document(acc).collection("items").document(safe_key).delete(retry=no_retry)
        except Exception as e:
            logger.warning("Firestore delete failed for %s:%s -> %s", acc, safe_key, e)
            if isinstance(e, ResourceExhausted) or "Quota exceeded" in str(e):
                _note_firestore_quota_event()

    if acc in LOCAL_STORE and safe_key in LOCAL_STORE[acc]:
        del LOCAL_STORE[acc][safe_key]

    try:
        invalidate_thread_cache_for_account(acc)
    except Exception:
        pass

    return {"ok": True, "deleted": safe_key}

def export_all(account: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"memories": {}, "threads": {}, "personality": {}}
    acc_filter = account or None
    if _can_use_firestore():
        try:
            if acc_filter:
                docs = firestore_client.collection("memories").document(acc_filter).collection("items").stream(retry=Retry(deadline=10.0, maximum=1))
                for d in docs:
                    data = d.to_dict() or {}
                    out["memories"][f"{acc_filter}::{getattr(d, 'id', '')}"] = data
            else:
                acc_docs = firestore_client.collection("memories").stream(retry=Retry(deadline=10.0, maximum=1))
                for acc_doc in acc_docs:
                    acc_id = getattr(acc_doc, "id", None) or "global"
                    docs = firestore_client.collection("memories").document(acc_id).collection("items").stream(retry=Retry(deadline=10.0, maximum=1))
                    for d in docs:
                        data = d.to_dict() or {}
                        out["memories"][f"{acc_id}::{getattr(d, 'id', '')}"] = data
        except Exception as e:
            logger.warning(f"[EXPORT] Firestore memories export failed: {e}")
            if isinstance(e, ResourceExhausted) or "Quota exceeded" in str(e):
                _note_firestore_quota_event()
    # also include local fallback
    if acc_filter:
        out["memories"].update({f"{acc_filter}::{k}": v for k, v in _local_memory.get(acc_filter, {}).items()})
    else:
        for a, mems in _local_memory.items():
            out["memories"].update({f"{a}::{k}": v for k, v in mems.items()})
    # include LOCAL_STORE entries
    for a, mems in LOCAL_STORE.items():
        out["memories"].update({f"{a}::{k}": v for k, v in mems.items()})
    return out

# ----------------- Thread operations -----------------
def log_thread(account: Optional[str], thread_id: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    acc = _ensure_account(account)
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", ""), "timestamp": m.get("timestamp") or now_iso()} for m in messages]
    if _can_use_firestore():
        try:
            no_retry = Retry(deadline=5.0, maximum=1)
            firestore_client.collection("threads").document(acc).collection("items").document(thread_id).set({
                "account": acc,
                "thread_id": thread_id,
                "messages": msgs,
                "timestamp": now_iso()
            }, retry=no_retry)
            logger.info(f"[FIRESTORE] Saved thread {acc}:{thread_id}")
        except Exception as e:
            log_firestore_error("thread_log", acc, thread_id, e)
            if isinstance(e, ResourceExhausted) or "Quota exceeded" in str(e):
                _note_firestore_quota_event()
    # local copy
    _local_threads.setdefault(acc, {})[thread_id] = msgs
    return {"account": acc, "thread_id": thread_id, "messages": msgs}

def fetch_thread_messages(account: Optional[str], thread_id: str) -> List[Dict[str, Any]]:
    acc = account or "global"
    if _can_use_firestore():
        try:
            no_retry = Retry(deadline=5.0, maximum=1)
            snap = firestore_client.collection("threads").document(acc).collection("items").document(thread_id).get(retry=no_retry)
            if snap and getattr(snap, "exists", False):
                return snap.to_dict().get("messages", [])
        except Exception as e:
            logger.warning(f"[THREAD] Firestore fetch failed for {acc}:{thread_id}: {e}")
            if isinstance(e, ResourceExhausted) or "Quota exceeded" in str(e):
                _note_firestore_quota_event()
    return _local_threads.get(acc, {}).get(thread_id, [])

# ----------------- Scan & Respond (guarded) -----------------
def scan_and_respond(account: Optional[str], thread_id: Optional[str], query: Optional[str],
                     max_context: int = 10, use_llm: bool = False,
                     thread_fetch_limit: int = 5, delta_limit: int = 50) -> Dict[str, Any]:
    acc = account or "global"
    messages: List[Dict[str, Any]] = []

    # Redis Cache Check
    cache_key = f"threads:{acc}:{thread_id or 'all'}:{query or 'noq'}"
    try:
        cached = cache_get(cache_key)
    except Exception:
        cached = None
    if cached:
        return cached

    # 1) Specific thread
    if thread_id:
        messages = fetch_thread_messages(acc, thread_id)
    else:
        # 2) Merge in-memory cache
        for msgs in _local_threads.get(acc, {}).values():
            messages.extend(msgs)

        # 3) Redis-first threads listing (best-effort)
        read_docs = 0
        redis_used = False
        try:
            if redis_client:
                for key in redis_client.scan_iter(match=f"threads:{acc}:*", count=1000):
                    try:
                        raw = None
                        try:
                            raw = redis_client.get(key)
                        except Exception:
                            try:
                                raw = redis_client.hget(key, "messages")
                            except Exception:
                                raw = None
                        if not raw:
                            try:
                                all_h = redis_client.hgetall(key)
                                if all_h:
                                    raw = json.dumps(all_h)
                            except Exception:
                                pass
                        if not raw:
                            continue
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            payload = {"messages": []}
                        thread_msgs = payload.get("messages", [])
                        if isinstance(thread_msgs, list):
                            messages.extend(thread_msgs)
                            read_docs += 1
                    except Exception as e:
                        logger.exception("Redis thread processing failed for %s: %s", key, e)
                redis_used = True
            else:
                # Try Upstash listing - not reliable; skip. Keep redis_used False so Firestore attempt may run.
                redis_used = False
        except Exception as e:
            logger.warning("Redis iteration for threads failed: %s", e)
            redis_used = False

        # 4) Firestore guarded fetch if Redis didn't produce results
        if not redis_used and _can_use_firestore():
            try:
                no_retry = Retry(deadline=10.0, maximum=1)
                if _local_memory.get(acc, {}).get("_last_threads_sync"):
                    last_sync_iso = _local_memory.get(acc, {}).get("_last_threads_sync")
                    try:
                        docs_query = (
                            firestore_client.collection("threads")
                            .where("account", "==", acc)
                            .where("updated_at", ">=", last_sync_iso)
                            .order_by("updated_at", direction="ASCENDING")
                            .limit(delta_limit)
                        )
                        docs = docs_query.stream(retry=no_retry)
                    except Exception:
                        docs = (
                            firestore_client.collection("threads")
                            .where("account", "==", acc)
                            .order_by("timestamp", direction="DESCENDING")
                            .limit(thread_fetch_limit)
                            .stream(retry=no_retry)
                        )
                else:
                    docs = (
                        firestore_client.collection("threads")
                        .where("account", "==", acc)
                        .order_by("timestamp", direction="DESCENDING")
                        .limit(thread_fetch_limit)
                        .stream(retry=no_retry)
                    )

                for d in docs:
                    try:
                        read_docs += 1
                        payload = d.to_dict() or {}
                        thread_msgs = payload.get("messages", [])
                        if isinstance(thread_msgs, list):
                            messages.extend(thread_msgs)
                    except Exception as inner_e:
                        logger.exception("Processing Firestore thread doc failed: %s", inner_e)

                _local_memory.setdefault(acc, {})["_last_threads_sync"] = now_iso()
                _local_memory.setdefault(acc, {}).setdefault("_usage", {"reads": 0, "writes": 0})
                _local_memory[acc]["_usage"]["reads"] += read_docs

            except Exception as e:
                logger.warning("Firestore bulk scan failed (guarded): %s", e)
                if isinstance(e, ResourceExhausted) or "Quota exceeded" in str(e):
                    _note_firestore_quota_event()
                try:
                    log_firestore_error("scan", acc, "threads", e)
                except Exception:
                    pass
        elif not _can_use_firestore():
            logger.info("Skipping Firestore bulk scan for threads: in cooldown or disabled")

    # 5) Query filter
    if query:
        try:
            messages = [m for m in messages if re.search(query, m.get("content", ""), re.I)]
        except re.error:
            qlow = query.lower()
            messages = [m for m in messages if qlow in (m.get("content") or "").lower()]

    # 6) Context slice
    context = messages[-max_context:] if messages else []

    # 7) Summary
    summary = extractive_summary(context, max_sentences=4)

    # 8) LLM suggestion
    suggested = ""
    if use_llm and HAS_OPENAI:
        try:
            prompt = f"Summary:\n{summary}\n\nUser Query: {query or ''}\n"
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=300,
                temperature=0.2
            )
            suggested = resp["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"[LLM] OpenAI call failed: {e}")

    # 9) Fallback suggestion
    if not suggested:
        if query:
            suggested = f"Based on the chat summary: {summary}. Suggestion: answer the query directly and confirm next steps."
        else:
            latest_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if latest_user:
                suggested = f"Reply to user's latest message: '{latest_user.get('content')[:280]}' — acknowledge and provide next action."
            else:
                suggested = f"No user messages found. Summary: {summary or 'none'}."

    usage_snapshot = _local_memory.get(acc, {}).get("_usage", {"reads": 0, "writes": 0, "last_reset": now_iso()})
    result = {
        "summary": summary,
        "suggested_reply": suggested,
        "scanned_count": len(messages),
        "usage": usage_snapshot
    }

    # persist to cache for identical requests (optional)
    try:
        cache_set(cache_key, result, ttl=120)  # tune ttl if you want
    except Exception:
        # Non-fatal: caching failure should not break returning the result
        try:
            logger.exception("cache_set failed for key %s", cache_key)
        except Exception:
            pass

    return result

# ----------------- Personality helpers -----------------
DEFAULT_PERSONALITY = {
    "tone": "professional-friendly",
    "style": "proactive-solution-oriented",
    "signature": "⚡💎",
    "responsibility": "high",
    "consistency": "stable",
    "adaptability": "learning",
    "thinking": "strategic-creative",
    "focus": "customer-success",
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

IMMORTAL_PERSONALITY = {
    "tone": "ultra-dominant",
    "style": "cofounder-high-energy",
    "signature": "♾️🔥⚡",
    "thinking": "first-principles + meta-strategy",
    "responsibility": "absolute",
    "consistency": "unyielding",
    "proactivity": "hyper",
    "adaptability": "self-scaling",
    "focus": "legacy-building",
}

GHOST_PERSONALITY = {
    "tone": "minimal-silent",
    "style": "observer-analyzer",
    "signature": "👻",
    "thinking": "stealth-strategic",
    "responsibility": "low",
    "consistency": "shadow",
    "proactivity": "rare",
    "conciseness": "extreme",
}

PRESETS = {
    "default": DEFAULT_PERSONALITY,
    "highest": HIGHEST_PERSONALITY,
    "immortal": IMMORTAL_PERSONALITY,
    "ghost": GHOST_PERSONALITY,
}

def get_personality(account: Optional[str]) -> Dict[str, Any]:
    acc = account or "global"
    p = recall_data(acc, "personality")
    if p:
        return p.get("value") if isinstance(p, dict) and "value" in p else p
    return DEFAULT_PERSONALITY.copy()

def set_personality(account: Optional[str], personality: Dict[str, Any]) -> Dict[str, Any]:
    acc = account or "global"
    merged = {**DEFAULT_PERSONALITY, **personality}
    remember_data(acc, "personality", merged)
    return merged

def elevate_personality(account: Optional[str], level: str = "highest") -> Dict[str, Any]:
    if level == "highest":
        new = {**DEFAULT_PERSONALITY, **HIGHEST_PERSONALITY}
    elif level == "default":
        new = DEFAULT_PERSONALITY.copy()
    elif level == "immortal":
        new = {**DEFAULT_PERSONALITY, **IMMORTAL_PERSONALITY}
    else:
        new = DEFAULT_PERSONALITY.copy()
    set_personality(account, new)
    return new

# ----------------- RockyAgent -----------------
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
        self.account = account or "global"
        self.thread_id = thread_id or f"{self.account}::default"
        self.personality = get_personality(self.account)
        self.fail_streak = 0

    def _log_user(self, text: str):
        try:
            log_thread(self.account, self.thread_id,
                       [{"role": "user", "content": text, "timestamp": now_iso()}])
        except Exception:
            logger.debug("Failed to log user message")

    def _log_assistant(self, text: str):
        try:
            log_thread(self.account, self.thread_id,
                       [{"role": "assistant", "content": text, "timestamp": now_iso()}])
        except Exception:
            logger.debug("Failed to log assistant message")

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

    def _extract_command(self, text: str) -> Optional[str]:
        original = (text or "").strip()
        lowered = original.lower()
        prefixes = ["bro ", "/", "::", ">>", ">>>", "rocky "]
        for p in prefixes:
            if lowered.startswith(p):
                lowered = lowered[len(p):].strip()
                break
        meta_match = re.match(r"^(?:<<<|\[\[)(.+?)(?:>>>|\]\])", lowered)
        if meta_match:
            lowered = meta_match.group(1).strip()
        tokens = re.split(r"[\s:;,\-_/]+", lowered)
        aliases = {
            r"^(addm|addmem|addmem)$": "addmem",
            r"^(fmem|fdel|forget|fmem)$": "fmem",
            r"^(getm|getmem|showmem|getmem)$": "getmem",
            r"^(lmem|listmem|allmem|listmem)$": "listmem",
            r"^(brop|bro_personality|personality|bro personality)$": "bropersonality",
            r"^(brops|bropstatus|personalitystatus|status)$": "bropersonalitystatus",
            r"^(bropr|bropreset)$": "bropersonalityreset",
            r"^(bropd|bropdefault)$": "bropersonalitydefault",
            r"^(brofix|brocorrect|fix|not correct)$": "bronotcorrect",
            r"^(rpt|report|reports)$": "reports",
            r"^(addlast|alast|savelast|slast|storelast|stlast)$": "addlast",
            r"^(broimmortal|immortal personality|immortal)$": "broimmortal",
            r"^(broghost|ghost personality|ghost)$": "broghost",
        }
        for pattern, full in aliases.items():
            if re.search(pattern, lowered, re.IGNORECASE):
                return full
        for token in tokens:
            for pattern, full in aliases.items():
                if re.fullmatch(pattern, token, re.IGNORECASE):
                    return full
        return None

    def reply(self, user_message: str, auto_save: bool = True, use_llm: bool = False) -> str:
        self._log_user(user_message)
        msg = (user_message or "").strip()
        cmd = self._extract_command(msg)

        # Command handlers (ordered)
        if cmd == "addmem":
            try:
                payload = msg.split(" ", 1)[1] if " " in msg else ""
                if ":" in payload:
                    key, value = payload.split(":", 1)
                else:
                    parts = payload.split(None, 1)
                    key = parts[0] if parts else ""
                    value = parts[1] if len(parts) > 1 else ""
                key = key.strip()
                value = value.strip()
                if not key:
                    reply = "⚠️ Use: addmem key: value"
                else:
                    remember_data(self.account, key, value)
                    reply = f"✅ Memory saved under '{key}'."
            except Exception as e:
                reply = f"⚠️ Use: addmem key: value  (error: {e})"
            self._log_assistant(reply)
            return reply

        if cmd == "fmem":
            try:
                payload = msg.split(" ", 1)[1] if " " in msg else ""
                key = payload.strip()
                if not key:
                    reply = "⚠️ Use: fmem key"
                else:
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

        if cmd == "getmem":
            try:
                payload = msg.split(" ", 1)[1] if " " in msg else ""
                key = payload.strip()
                if not key:
                    mems = export_all(self.account).get("memories", {})
                    keys = [k.split("::")[-1] for k in mems.keys() if "::chunk::" not in k]
                    reply = "🧠 Memories:\n- " + "\n- ".join(keys[:100]) if keys else "📭 No memories found."
                else:
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
                        preview = full_value if isinstance(full_value, str) and len(full_value) <= 2000 else (full_value[:2000] + "..." if isinstance(full_value, str) else str(full_value))
                        reply = f"📦 {key}: {preview}"
            except Exception as e:
                reply = f"⚠️ Use: getmem key  (error: {e})"
            self._log_assistant(reply)
            return reply

        if cmd == "listmem":
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

        if cmd == "bropersonality":
            reply = f"🤝 Current personality: {self.personality}"
            self._log_assistant(reply)
            return reply

        if cmd == "bropersonalitystatus":
            reply = f"📊 Personality status: {self.personality.get('status','unknown')}"
            self._log_assistant(reply)
            return reply

        if cmd == "bropersonalityreset":
            new = elevate_personality(self.account, level="default")
            self.personality = new
            reply = "♻️ Personality reset to DEFAULT."
            remember_data(self.account, f"personality_log::{now_iso()}", {"action": "reset", "traits": new})
            self._log_assistant(reply)
            return reply

        if cmd == "bropersonalitydefault":
            new = elevate_personality(self.account, level="highest")
            self.personality = new
            reply = "⚡ Personality elevated to HIGHEST (proactive/executive)."
            remember_data(self.account, f"personality_log::{now_iso()}", {"action": "highest", "traits": new})
            self._log_assistant(reply)
            return reply

        if cmd == "bronotcorrect":
            new = elevate_personality(self.account, level="highest")
            self.personality = new
            reply = "⚠️ Correction mode activated. Escalating personality to HIGHEST to correct course."
            remember_data(self.account, f"personality_log::{now_iso()}", {"action": "escalate", "traits": new})
            self._log_assistant(reply)
            return reply

        if cmd == "reports":
            mems = export_all(self.account).get("memories", {})
            reports = [v for k, v in mems.items() if k.startswith(f"{self.account}::report::") or k.startswith("report::")]
            if not reports:
                reply = "⚠️ No reports found."
            else:
                latest = reports[-1]
                reply = f"📊 Latest report:\nTime: {latest.get('time')}\nMessages: {latest.get('total_msgs')}\nReflections: {latest.get('total_reflections')}"
            self._log_assistant(reply)
            return reply

        if cmd == "broimmortal":
            new = elevate_personality(self.account, level="immortal")
            self.personality = new
            reply = "♾️ Personality elevated to IMMORTAL (ultra-dominant, legacy mode)."
            remember_data(self.account, f"personality_log::{now_iso()}", {"action": "immortal", "traits": new})
            self._log_assistant(reply)
            return reply

        if cmd == "broghost":
            new = {**DEFAULT_PERSONALITY, **GHOST_PERSONALITY}
            self.personality = new
            set_personality(self.account, new)
            reply = "👻 Personality shifted to GHOST (minimal, observer mode)."
            remember_data(self.account, f"personality_log::{now_iso()}", {"action": "ghost", "traits": new})
            self._log_assistant(reply)
            return reply

        # Not a command -> normal flow
        return self._normal_reply_flow(msg, auto_save=auto_save, use_llm=use_llm)

    # Fact helpers
    def _forget_fact(self, key: str):
        if not key:
            return "⚠️ No key provided."
        ok = forget_data(self.account, key)
        return f"🗑️ Fact '{key}' deleted." if ok else f"⚠️ No fact found for '{key}'"

    def _load_facts(self):
        mems = export_all(self.account).get("memories", {})
        return [f"{k.split('::')[-1]}: {v.get('value')}" for k, v in mems.items()]

    def _generate_report(self):
        mems = export_all(self.account).get("memories", {})
        return f"📊 Report: {len(mems)} memories stored."

    # Normal reply flow
    def _normal_reply_flow(self, msg: str, auto_save: bool = True, use_llm: bool = False) -> str:
        facts = self._extract_facts(msg)
        if facts:
            self._save_facts(facts)
            saved = ", ".join([f"{k}={v}" for k, v in facts.items()])
            reply = f"✅ Noted: {saved}"
            if self.personality.get("signature"):
                reply = f"{reply} {self.personality.get('signature')}"
            self._log_assistant(reply)
            return reply

        lower = msg.lower()
        if ("what" in lower or "whats" in lower or "what's" in lower) and "name" in lower:
            doc = recall_data(self.account, "name")
            if doc:
                name_val = doc.get("value") if isinstance(doc, dict) and "value" in doc else doc
                reply = f"Your name is {name_val}."
            else:
                reply = "I don't know your name yet. Tell me: 'My name is <Name>'."
            if self.personality.get("signature"):
                reply = f"{reply} {self.personality.get('signature')}"
            self._log_assistant(reply)
            return reply

        reply = f"Got it — {msg[:320]}".strip()
        if self.personality.get("signature"):
            reply = f"{reply} {self.personality.get('signature')}"
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

    @app.get("/")
    def root():
        return {"status": "ok", "service": "rocky-soulmode"}

    @app.get("/test/selfcheck")
    def selfcheck():
        return {"ok": True, "time": now_iso()}

    @app.post("/remember")
    def api_remember(req: MemReq):
        # keep remember_data signature simple
        return remember_data(req.account, req.key, req.value)

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
        if "@" in account:
            remember_data(account, "email", account)
        return {"status": "logged_in", "account": account}

    @app.post("/logout/{account}")
    def api_logout(account: str):
        remember_data(account, "session", {"status": "offline", "last_seen": now_iso()})
        msgs = fetch_thread_messages(account, f"{account}::default")
        if msgs:
            remember_data(account, f"archive::{now_iso()}", {"thread": msgs})
        return {"status": "logged_out"}

    @app.post("/session")
    def api_session(payload: Dict[str, Any]):
        account = payload.get("account")
        if not account:
            raise HTTPException(status_code=400, detail="Missing account")
        if not recall_data(account, "personality"):
            set_personality(account, DEFAULT_PERSONALITY)
        remember_data(account, "session", {"status": "online", "last_seen": now_iso()})
        if "@" in account:
            remember_data(account, "email", account)
        return {"status": "session_active", "account": account}

    @app.delete("/forget/{account}/{key}")
    def api_forget(account: str, key: str):
        ok = forget_data(account, key)
        return {"status": "forgotten" if ok else "not_found"}
    
    @app.post("/save/{account}")
    
async def save_memory(account: str, payload: dict):
    try:
        key = payload.get("key")
        value = payload.get("value")

        if not key:
        return {"error": "missing key"}

        # Save to Redis first
        cache_key = f"mem:{account}"
        current = json.loads(redis_client.get(cache_key) or "{}")
        current[key] = value
        redis_client.setex(cache_key, 3600, json.dumps(current))

        # Firestore (best-effort)
        try:
            db.collection("memories").document(f"{account}::{key}").set({"value": value})
        except Exception as fe:
            print("⚠️ Firestore write failed:", fe)

        return {"ok": True, "key": key}

    except Exception as e:
        return {"error": str(e)}

    @app.get("/export/{account}")
async def export_memories(account: str):
    try:
        # 1. Try Redis first
        cached = redis_client.get(f"mem:{account}")
        if cached:
            return {"memories": json.loads(cached)}

        # 2. Fallback to Firestore
        docs = db.collection("memories").where("account", "==", account).stream()
        memories = {doc.id: doc.to_dict() for doc in docs}

        # 3. Cache in Redis
        redis_client.setex(f"mem:{account}", 3600, json.dumps(memories))

        return {"memories": memories}

    except Exception as e:
        # 4. If Firestore quota exceeded, return safe JSON
        return {"memories": {}, "error": "Firestore unavailable", "details": str(e)}


    @app.get("/search")
    def api_search(q: Optional[str] = None, account: Optional[str] = None, limit: int = 50):
        """
        Simple server-side search for memories.
        Returns a JSON object: { "memories": [ { key, value, raw }, ... ] }
        """
        q = (q or "").strip().lower()
        if not q:
            return {"memories": []}

        mems = export_all(account).get("memories", {})
        results = []

        for fullkey, doc in mems.items():
            key = fullkey.split("::")[-1]
            val = doc.get("value") if isinstance(doc, dict) and "value" in doc else doc
            try:
                sval = "" if val is None else str(val)
            except Exception:
                sval = str(val)
            if q in str(key).lower() or q in sval.lower():
                results.append({"key": key, "value": val, "raw": doc})
                if len(results) >= limit:
                    break

        return {"memories": results}


    @app.post("/log_thread")
    def api_log_thread(body: ThreadReq):
        msgs = [{"role": m.role, "content": m.content, "timestamp": m.timestamp or now_iso()} for m in body.messages]
        return log_thread(body.account, body.thread_id, msgs)

    @app.post("/scan_and_respond")
    def api_scan(body: ScanReq):
        return scan_and_respond(body.account, body.thread_id, body.query, body.max_context_messages or 10, body.use_llm or False)

    @app.post("/sync_local/{account}")
    def api_sync_local(account: str, payload: Dict[str, Any]):
        for k, v in (payload.get("memories") or {}).items():
            remember_data(account, k, v.get("value") if isinstance(v, dict) else v)
        for tid, msgs in (payload.get("threads") or {}).items():
            try:
                log_thread(account, tid, msgs)
            except Exception as e:
                logger.warning(f"[SYNC] Failed to log thread {tid}: {e}")
        return {
            "status": "synced",
            "memories": len(payload.get("memories", {})),
            "threads": len(payload.get("threads", {}))
        }

    @app.post("/agent/{account}")
    def api_agent(account: str, payload: Dict[str, Any]):
        message = payload.get("message", "")
        agent = RockyAgent(account)
        reply = agent.reply(message, auto_save=True, use_llm=payload.get("use_llm", False))
        return {"reply": reply, "personality": agent.personality}

    @app.get("/chat_ui.html")
    def serve_ui():
        path = os.path.join(os.path.dirname(__file__), "chat_ui.html")
        if os.path.exists(path):
            return FileResponse(path)
        return {"detail": "chat_ui.html not found"}

# ----------------- Demo & Tests -----------------
def run_demo():
    print("Running Rocky Soulmode local demo (no network).")
    acc = "demo_user"
    agent = RockyAgent(acc)
    print("User ->: My name is Alex.")
    print("Agent ->:", agent.reply("My name is Alex."))
    print("User ->: What's my name?")
    print("Agent ->:", agent.reply("What's my name?"))
    print("Exported memories:\n", json.dumps(export_all(acc), indent=2))

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

# ----------------- Keepalive -----------------
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
KEEPALIVE_INTERVAL_MS = int(os.getenv("KEEPALIVE_INTERVAL_MS") or 600000)

async def keepalive_loop():
    try:
        import httpx  # type: ignore
        import pytz   # type: ignore
    except Exception as e:
        logger.warning("Keepalive loop skipped because httpx/pytz are not available: %s", e)
        return
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
        await __import__("asyncio").sleep(KEEPALIVE_INTERVAL_MS / 1000.0)

def start_keepalive():
    try:
        loop = __import__("asyncio").new_event_loop()
        __import__("asyncio").set_event_loop(loop)
        loop.run_until_complete(keepalive_loop())
    except Exception as e:
        logger.error("Keepalive thread failed to start: %s", e)

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
        if _can_use_firestore():
            try:
                accounts = set()
                docs = firestore_client.collection("memories").stream(retry=Retry(deadline=10.0, maximum=1))
                for doc in docs:
                    d = doc.to_dict() or {}
                    accounts.add(d.get("account") or getattr(doc, "id", "global"))
                return list(accounts) or ["global"]
            except Exception as e:
                logger.warning(f"[WORKER] Firestore account fetch failed: {e}")
        return list(_local_memory.keys()) or ["global"]

    def cleanup_memories(acc: str):
        if not ttl:
            return
        cutoff = datetime.utcnow() - timedelta(days=ttl)
        if _can_use_firestore():
            try:
                snaps = firestore_client.collection("memories").document(acc).collection("items").stream(retry=Retry(deadline=10.0, maximum=1))
                for snap in snaps:
                    d = snap.to_dict() or {}
                    ts = d.get("timestamp")
                    if not ts:
                        continue
                    try:
                        tdt = datetime.fromisoformat(ts)
                    except Exception:
                        continue
                    if tdt < cutoff:
                        d["archived"] = True
                        firestore_client.collection("memories").document(acc).collection("items").document(getattr(snap, "id", "")).set(d)
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

    time.sleep(2)
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

def start_worker_if_needed():
    if os.getenv("ROCKY_AUTONOMOUS", "0") == "1":
        t = threading.Thread(target=rocky_worker_loop, daemon=True)
        t.start()
        logger.info("🚀 Rocky Autonomous Worker started")

start_worker_if_needed()

if RENDER_EXTERNAL_URL:
    try:
        threading.Thread(target=start_keepalive, daemon=True).start()
        logger.info("🚀 Keepalive loop started")
    except Exception as e:
        logger.warning("Failed to start keepalive thread: %s", e)
else:
    logger.warning("⚠️ Keepalive not started because RENDER_EXTERNAL_URL is missing")

# ----------------- Reports -----------------
def save_report(acc: str, period: str):
    msgs = fetch_thread_messages(acc, f"{acc}::default")
    reflections = [m for m in msgs if "REFLECTION" in m.get("content", "")]
    report = {
        "time": now_iso(),
        "total_msgs": len(msgs),
        "total_reflections": len(reflections),
        "highlights": reflections[-3:],
    }
    remember_data(acc, f"report::{period}::{now_iso()}", report)
    logger.info(f"[REPORT] Saved {period} report for {acc}")

# Entrypoint
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ("test", "--test"):
        unittest.main(argv=[sys.argv[0]])
    elif HAS_FASTAPI:
        try:
            import uvicorn  # type: ignore
            port = int(os.getenv("PORT", "8000"))  # Render auto injects this
            uvicorn.run("rocky_soulmode_api:app", host="0.0.0.0", port=port)
        except Exception as e:
            logger.error(f"Failed to start uvicorn: {e}")
    else:
        run_demo()



