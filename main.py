from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MT5 AI Optimizer API")

@app.get("/")
def read_root():
    return {"message": "Welcome to MT5 AI Optimizer API"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
