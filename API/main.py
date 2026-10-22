from fastapi import FastAPI
from API.schemas import FraudRequest, FraudResponse
from API.services import predict_fraud

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(
    title="Fraud Detection API",
    description="Predict fraudulent transactions using MLflow-registered model",
    version="1.0.0"
)

# Expose /metrics endpoint for Prometheus
Instrumentator().instrument(app).expose(app)

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/predict", response_model=FraudResponse)
def predict(request: FraudRequest):
    prediction = predict_fraud(request.dict())
    return FraudResponse(prediction=prediction)
