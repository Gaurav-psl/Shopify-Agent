from fastify import FastAPI

app = FastAPI(
    title = "Shopify AI Agent",
    description = "Backend for Shopify AI"
    version = "1.0.0"
)

@app.get("/")
def home():
    return{
        "status":"running",
        "message":"shopify Ai Agent Backend is Running!"
    }

@app.get("/health")
def health():
    return{
        "status":"healthy"
    }