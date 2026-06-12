# ── API IA (FastAPI + scikit-learn) ──
FROM python:3.12-slim
WORKDIR /app

# Dépendances en couche cache séparée
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif (module d'entrée : app.main:app)
COPY app ./app

# Service INTERNE (jamais exposé sur internet), appelé par le backend Java
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
