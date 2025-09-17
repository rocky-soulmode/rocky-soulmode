import os
from google.cloud import firestore

print("🔍 Using Firestore project via credentials:", os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))

db = firestore.Client()

# Insert a test doc
doc_ref = db.collection("memories").document("firestore_debug")
doc_ref.set({
    "account": "global",
    "key": "firestore_debug",
    "value": "hello_firestore"
})
print("✅ Inserted test memory into Firestore.")

# Fetch it back
doc = doc_ref.get()
print("📦 Fetched from Firestore:", doc.to_dict())
