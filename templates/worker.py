# worker.py
import time
from app import app, scheduler  # imports your Flask app and the APScheduler you already set up

if __name__ == "__main__":
    # Make sure the app context is available to scheduled jobs (DB, mail, etc.)
    with app.app_context():
        # Start the scheduler loop (it will run your @scheduler.task jobs)
        scheduler.start()
        print("✅ TerraSentinel worker started. Hourly digests + ingestion are active.")

        # Keep the process alive forever
        while True:
            time.sleep(3600)
