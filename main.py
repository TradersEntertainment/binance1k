import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"[SERVER] Starting Binance Wall Alert Dashboard on port {port}...")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
