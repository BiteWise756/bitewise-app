import os
import json
import firebase_admin
from firebase_admin import credentials, auth, firestore, storage
from dotenv import load_dotenv

load_dotenv()

firebase_app = None
db = None
bucket = None

def init_firebase():
    global firebase_app, db, bucket

    if firebase_app:
        return firebase_app

    try:
        cred_dict = {
            "type": os.getenv("FIREBASE_TYPE", "service_account"),
            "project_id": os.getenv("FIREBASE_PROJECT_ID"),
            "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n"),
            "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "client_id": os.getenv("FIREBASE_CLIENT_ID"),
            "auth_uri": os.getenv("FIREBASE_AUTH_URI"),
            "token_uri": os.getenv("FIREBASE_TOKEN_URI"),
            "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_X509_CERT_URL"),
            "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_X509_CERT_URL"),
        }

        cred = credentials.Certificate(cred_dict)
        firebase_app = firebase_admin.initialize_app(cred, {
            "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET"),
            "databaseURL": os.getenv("FIREBASE_DATABASE_URL"),
        })

        db = firestore.client()
        bucket = None #bucket = storage.bucket()

        print("Firebase initialized successfully")
        return firebase_app

    except Exception as e:
        print(f"Firebase initialization error: {e}")
        firebase_app = None
        return None

def get_db():
    global db
    if db is None:
        init_firebase()
    return db

def get_bucket():
    global bucket
    if bucket is None:
        init_firebase()
    return bucket

def verify_token(id_token):
    try:
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token
    except Exception as e:
        print(f"Token verification error: {e}")
        return None
