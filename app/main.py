from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import waitlist
from app.database import engine, Base
import uvicorn

app = FastAPI(title="Fundry API", version="1.0")

# CORS - Allow Vercel frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://fundry.vercel.app", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(waitlist.router)

# Health / Ping endpoint for Render
@app.get("/ping")
async def ping():
    return {"status": "alive"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
