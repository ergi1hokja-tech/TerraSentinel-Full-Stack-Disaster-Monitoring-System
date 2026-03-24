import os
import pandas as pd
from datetime import datetime
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sklearn.tree import DecisionTreeClassifier
from sklearn.preprocessing import LabelEncoder
import joblib

# Initialize a minimal Flask app for DB access
app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'data.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Import your existing model from app
class DisasterEvent(db.Model):
    __tablename__ = 'disaster_events'
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    date = db.Column(db.Date, nullable=False)
    severity = db.Column(db.String(20), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)

MODEL_PATH = os.path.join(basedir, "risk_model.pkl")

def train_risk_model():
    """Train or update a simple AI model for disaster risk prediction."""
    with app.app_context():
        events = DisasterEvent.query.all()
        if not events:
            print("⚠️ No events found in DB to train model.")
            return
        
        data = [{
            "type": e.type,
            "month": e.date.month,
            "severity": e.severity
        } for e in events]
        df = pd.DataFrame(data)

        severity_map = {"Low": 1, "Medium": 2, "High": 3, "Extreme": 4}
        df["severity_num"] = df["severity"].map(severity_map)

        le = LabelEncoder()
        df["type_encoded"] = le.fit_transform(df["type"])

        X = df[["type_encoded", "month"]]
        y = df["severity_num"]

        model = DecisionTreeClassifier(random_state=42)
        model.fit(X, y)

        joblib.dump({"model": model, "encoder": le}, MODEL_PATH)
        print(f"✅ Risk model trained and saved to {MODEL_PATH}")

if __name__ == "__main__":
    train_risk_model()
