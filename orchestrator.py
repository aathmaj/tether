import ngrok 
import uvicorn
from fastapi import FastAPI, UploadFile, File

from fastapi.responses import FileResponse
import shutil
import os
from dotenv import load_dotenv
load_dotenv()


NGROK_AUTH_TOKEN = os.getenv("NGROK_AUTH_TOKEN")


PORT = 8000

app = FastAPI()
os.makedirs("./jobs", exist_ok=True)
os.makedirs("./results", exist_ok=True)

@app.get("/get_job")
def get_job():
    return {"file_name": "ball-in-grass.blend", "start": 1, "end": 5}

@app.get("/download_job/{filename}")
def download_job(filename: str):
    return FileResponse(os.path.join("./jobs", filename))

@app.post("/upload_result")
async def upload_result(file: UploadFile = File(...)):
    with open(f"./results/{file.filename}", "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return {"status": "success"}

if __name__ == "__main__":
    
    ngrok.set_auth_token(NGROK_AUTH_TOKEN)
    listener = ngrok.forward(PORT)
    
    print(f"\nLive")
    print(f" {listener.url()}")
    print(f"--------------------------------------------\n")

  
    uvicorn.run(app, host="127.0.0.1", port=PORT)