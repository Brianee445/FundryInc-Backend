from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import waitlist
from app.database import engine, Base
import uvicorn

app = FastAPI(title="Fundry API", version="1.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://fundry.vercel.app", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(waitlist.router)

# Health / ping
@app.get("/ping")
async def ping():
    return {"status": "alive"}

@app.get("/health")
async def health():
    return {"status": "ok"}

# Create tables on startup (synchronous)
@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
