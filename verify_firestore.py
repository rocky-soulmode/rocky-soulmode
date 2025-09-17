# verify_firestore.py
import os
import time
import json
from datetime import datetime
from google.oauth2 import service_account
from google.cloud import firestore

def safe_print(x):
    try:
        print(x)
    except Exception:
        print(str(x))

def main():
    cred_env = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    safe_print(f"GOOGLE_APPLICATION_CREDENTIALS env: {cred_env}")

    creds = None
    client = None
    try:
        if cred_env and os.path.exists(cred_env):
            creds = service_account.Credentials.from_service_account_file(
                cred_env,
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            proj = getattr(creds, "project_id", None)
            safe_print(f"Loaded service account file. project_id in creds: {proj}")
            client = firestore.Client(credentials=creds, project=proj)
        else:
            safe_print("No service account file found at GOOGLE_APPLICATION_CREDENTIALS; falling back to ADC / default client")
            client = firestore.Client()
    except Exception as e:
        safe_print(f"Failed to create Firestore client: {e}")
        return

    try:
        safe_print(f"Effective client.project: {client.project}")
    except Exception as e:
        safe_print(f"Could not read client.project: {e}")

    # list top-level collections
    safe_print("\n--- TOP LEVEL COLLECTIONS ---")
    try:
        for col in client.collections():
            safe_print(f"collection: {col.id}")
    except Exception as e:
        safe_print(f"Error listing collections: {e}")

    # Show documents under 'memories' and 'threads' explicitly
    for colname in ("memories", "threads"):
        safe_print(f"\n--- DOCUMENTS in collection '{colname}' ---")
        try:
            col = client.collection(colname)
            docs = list(col.stream())
            if not docs:
                safe_print(f"(no documents found in '{colname}')")
            for d in docs:
                safe_print(f"DOC ID: {d.id}")
                safe_print(json.dumps(d.to_dict(), indent=2, default=str))
                safe_print("-" * 40)
        except Exception as e:
            safe_print(f"Failed reading collection '{colname}': {e}")

    # Write a short test document and read it back
    ts = int(time.time())
    test_doc_id = f"diag-test::{ts}"
    test_doc = {
        "account": "diag",
        "key": "diag-test",
        "value": f"diag ok {datetime.utcnow().isoformat()}",
        "tags": ["diag"],
        "timestamp": datetime.utcnow().isoformat()
    }
    safe_print(f"\n--- Writing test doc to memories/{test_doc_id} ...")
    try:
        client.collection("memories").document(test_doc_id).set(test_doc)
        safe_print("Write succeeded.")
    except Exception as e:
        safe_print(f"Write FAILED: {e}")
        return

    safe_print("\n--- Reading it back ---")
    try:
        snap = client.collection("memories").document(test_doc_id).get()
        if snap.exists:
            safe_print("Read OK:")
            safe_print(json.dumps(snap.to_dict(), indent=2, default=str))
        else:
            safe_print("Read: document does NOT exist (weird).")
    except Exception as e:
        safe_print(f"Read FAILED: {e}")

    safe_print("\nDone. If the write & read succeeded, Firestore is reachable and the doc is in the project above.")
    safe_print("If you still don't see it in Firebase Console, confirm you opened the exact same project id and logged in with the same Google account.")

if __name__ == "__main__":
    main()
input("\nPress ENTER to exit...")
