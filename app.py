from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Database config — uses PostgreSQL in production, SQLite locally
database_url = os.environ.get('DATABASE_URL', 'sqlite:///local.db')
# Railway sometimes gives postgres:// — SQLAlchemy needs postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ── MODEL ────────────────────────────────────────────────
class FailedJob(db.Model):
    __tablename__ = 'failed_jobs'
    id          = db.Column(db.Integer, primary_key=True)
    payload     = db.Column(db.JSON, nullable=False)
    error       = db.Column(db.String(500))
    retry_count = db.Column(db.Integer, default=3)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id":          self.id,
            "payload":     self.payload,
            "error":       self.error,
            "retry_count": self.retry_count,
            "timestamp":   self.created_at.isoformat()
        }

with app.app_context():
    db.create_all()

# ── RETRY LOGIC ──────────────────────────────────────────
BACKOFF_SCHEDULE = [1, 5, 30]  # seconds between retries
TARGET_URL = os.environ.get('TARGET_URL', 'https://httpbin.org/post')

def attempt_delivery(payload):
    """
    Tries to deliver payload to TARGET_URL.
    Retries on transient errors (429, 502, 503) with exponential backoff.
    Returns (success: bool, error: str or None)
    """
    for attempt, wait in enumerate(BACKOFF_SCHEDULE, start=1):
        try:
            response = requests.post(TARGET_URL, json=payload, timeout=5)

            if response.status_code == 200:
                return True, None

            elif response.status_code in [429, 502, 503]:
                # Transient — wait and retry
                print(f"Attempt {attempt}: got {response.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue

            else:
                # Fatal — don't retry
                return False, f"Fatal error: HTTP {response.status_code}"

        except requests.exceptions.Timeout:
            print(f"Attempt {attempt}: timeout, retrying in {wait}s")
            time.sleep(wait)

        except requests.exceptions.ConnectionError as e:
            return False, f"Connection error: {str(e)}"

    return False, "Max retries exceeded after 3 attempts"

# ── ROUTES ───────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "webhook-retry-api"}), 200


@app.route('/ingest', methods=['POST'])
def ingest():
    # Validate JSON
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    # Validate required fields
    if "event" not in payload or "data" not in payload:
        return jsonify({
            "error": "Missing required fields",
            "required": ["event", "data"]
        }), 422

    # Attempt delivery with retry logic
    success, error = attempt_delivery(payload)

    if success:
        return jsonify({"status": "delivered", "event": payload["event"]}), 200

    # Delivery failed — write to dead-letter queue
    failed_job = FailedJob(
        payload=payload,
        error=error,
        retry_count=len(BACKOFF_SCHEDULE)
    )
    db.session.add(failed_job)
    db.session.commit()

    return jsonify({
        "status":  "failed",
        "error":   error,
        "message": "Payload logged to dead-letter queue. Check GET /failures"
    }), 500


@app.route('/failures', methods=['GET'])
def get_failures():
    jobs = FailedJob.query.order_by(FailedJob.created_at.desc()).all()
    return jsonify({
        "count":    len(jobs),
        "failures": [j.to_dict() for j in jobs]
    }), 200


@app.route('/failures/<int:job_id>', methods=['DELETE'])
def clear_failure(job_id):
    job = FailedJob.query.get_or_404(job_id)
    db.session.delete(job)
    db.session.commit()
    return jsonify({"status": "deleted", "id": job_id}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)