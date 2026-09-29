# Medical AI System + BiteWise Nutrition Assistant

A dual-application project combining an AI-powered medical diagnosis dashboard with a nutrition tracking assistant.

---

## Project Structure

```
project-root/
├── backend/                  # FastAPI backend server
│   ├── app.py               # Main server entry point
│   ├── routes/              # API route handlers
│   ├── services/            # Business logic (Gemini, SigLIP, nutrition, etc.)
│   ├── models/              # Pydantic schemas
│   ├── utils/               # Helper functions
│   ├── firebase/            # Firebase configuration
│   ├── requirements.txt     # Python dependencies
│   └── .env                 # Environment variables (create this)
├── medical_dashboard_v2.html  # Medical AI dashboard frontend
├── bitewise-app.html          # BiteWise nutrition assistant frontend
├── models/                    # XGBoost ML model files
│   └── models_data.json      # Extracted model trees (loaded at runtime)
├── sample_breast.csv          # Sample patient data for testing
├── sample_liver.csv           # Sample patient data for testing
└── extract_models.py          # Script to extract XGBoost tree data
```

---

## Requirements

- **Python 3.10+** (3.12 recommended)
- **Ollama** (for AI features) — [ollama.ai](https://ollama.ai)
- **Node.js** (optional, only if using npm-based tools)

---

## Backend Setup

### 1. Create and activate a virtual environment

**Windows (PowerShell / CMD):**
```cmd
cd backend
python -m venv venv
venv\Scripts\activate
```

**macOS / Linux:**
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a file named `.env` inside the `backend/` folder with the following content:

```env
# Ollama AI (local LLM)
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=gemma4:cloud

# Firebase (optional — needed for BiteWise auth)
FIREBASE_TYPE=service_account
FIREBASE_PROJECT_ID=your-project-id
FIREBASE_PRIVATE_KEY_ID=your-key-id
FIREBASE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
FIREBASE_CLIENT_EMAIL=your-client-email
FIREBASE_CLIENT_ID=your-client-id
FIREBASE_API_KEY=your-api-key
# ... other Firebase config values
```

> **Note:** Ollama is required for the Doctor Assistant AI feature. Firebase is only needed for BiteWise user authentication. Without Firebase, the XGBoost ML models and doctor assistant still work.

### 4. Install and run Ollama (for AI features)

```bash
# Install Ollama from https://ollama.ai, then:
ollama pull gemma4:cloud
ollama serve
```

### 5. Start the backend server

```bash
cd backend
venv\Scripts\activate        # Windows
# or: source venv/bin/activate   # macOS/Linux
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at `http://localhost:8000`.

---

## Running the Frontend (HTML Apps)

The frontend files are standalone HTML pages that call the backend API. Serve them with any HTTP server:

### Option A: Python HTTP server

```bash
# From the project root directory
python -m http.server 8080
```

### Option B: VS Code Live Server

Right-click the HTML file → "Open with Live Server"

### Access the apps

| App | URL |
|-----|-----|
| Medical Dashboard | `http://localhost:8080/medical_dashboard_v2.html` |
| BiteWise Nutrition | `http://localhost:8080/bitewise-app.html` |
| Backend API | `http://localhost:8000` |
| Backend Health | `http://localhost:8000/health` |
| Doctor AI Health | `http://localhost:8000/doctor/health` |

---

## How to Use

### Medical Dashboard
1. Open `medical_dashboard_v2.html` in a browser
2. Click **Upload CSV** and select a sample CSV (e.g., `sample_breast.csv`)
3. View predictions, charts, and analytics
4. Navigate to **🩺 Doctor AI** tab for AI-powered case analysis

### Doctor Assistant AI
1. Navigate to the **🩺 Doctor AI** page (sidebar)
2. Enter patient symptoms, test results, and examination notes
3. Click **Analyze Case**
4. The AI returns: possible diagnosis, explanation, causes, next steps, treatment, and medications

### BiteWise Nutrition
1. Open `bitewise-app.html`
2. Sign in or create an account
3. Use the **AI Meal Scanner** to analyze food photos
4. Track nutrition, water intake, and health goals
5. Your **User ID** (e.g. `USR-A7K2M`) is shown in the **More** tab — share this with doctors

### Cross-App: Patient Lookup (Medical Dashboard)
1. Open `medical_dashboard_v2.html`
2. Click the **👤 Patient Lookup** sidebar icon
3. Enter a patient's User ID (`USR-XXXXX`) or Firebase UID
4. View their profile, nutrition stats, recent meals, weight history, AI coach conversations, and doctor notes

### User Account System
- Every user gets a unique `display_id` (e.g. `USR-A7K2M`) on account creation
- Existing users get one automatically on next login or profile fetch
- Share the `display_id` with healthcare providers so they can look up your data
- AI Nutrition Coach conversations are saved to Firestore automatically
- Doctors can add notes from the dashboard via the API

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Backend won't start | Ensure port 8000 is free. Check `python --version` is 3.10+. |
| Doctor AI returns connection error | Make sure `ollama serve` is running and `gemma4:cloud` is pulled: `ollama pull gemma4:cloud` |
| CSV upload shows demo mode | The XGBoost model data needs to be extracted: `python extract_models.py` |
| BiteWise auth fails | Firebase credentials in `.env` may be missing or invalid. |
| CORS errors in browser | Serve the HTML files via HTTP server (not `file://`), or use `--allow-file-access-from-files` |
