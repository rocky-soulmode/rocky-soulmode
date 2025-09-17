import os
from google.oauth2 import service_account
from google.cloud import firestore

cred_path = r"C:\Users\kawshik\Documents\Chatgpt\chatgpt-kaapav-472214-894825d83fbb.json"
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path

creds = service_account.Credentials.from_service_account_file(cred_path)
client = firestore.Client(credentials=creds, project=creds.project_id)

print("Connected to project:", creds.project_id)
col = client.collection("memories")
docs = list(col.stream())

print("Documents found in 'memories':", len(docs))
for d in docs:
    print("DOC ID:", d.id)
    print(d.to_dict())
    print("-" * 40)

# Jugaad: keep window open
input("\nPress ENTER to exit...")
