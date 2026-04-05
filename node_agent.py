import subprocess
import os
import requests
import zipfile
import time

from dotenv import load_dotenv
load_dotenv()


ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL")

IMAGE_NAME = "linuxserver/blender:latest"
LOCAL_DIR = os.path.abspath("./node_data")

def setup_node():
    if not os.path.exists(LOCAL_DIR):
        os.makedirs(LOCAL_DIR)
    
    print("Checking Docker status...")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
    except:
        print("ERROR: Docker is not running. Please start Docker Desktop.")
        return False
    return True

def run_sandboxed_job():
    try:
        # job
        print(f"Connecting to Orchestrator at {ORCHESTRATOR_URL}...")
        job = requests.get(f"{ORCHESTRATOR_URL}/get_job", timeout=10).json()
        filename = job['file_name']
        
        # download
        print(f"Downloading {filename}...")
        r = requests.get(f"{ORCHESTRATOR_URL}/download_job/{filename}")
        with open(os.path.join(LOCAL_DIR, filename), 'wb') as f:
            f.write(r.content)

        # render
        print("Starting Sandboxed Render. This may take a few minutes...")
        # Note: -v maps the local folder to /data inside the container
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{LOCAL_DIR}:/data",
            IMAGE_NAME,
            "blender", "-b", f"/data/{filename}",
            "-o", "/data/frame_####",
            "-s", str(job['start']), "-e", str(job['end']), "-a"
        ]
        subprocess.run(docker_cmd, check=True)

        # zipping
        zip_path = os.path.join(LOCAL_DIR, "results.zip")
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for file in os.listdir(LOCAL_DIR):
                if file.startswith("frame_") and file.endswith(".png"):
                    zipf.write(os.path.join(LOCAL_DIR, file), file)

        # upload
        print("Uploading finished frames...")
        with open(zip_path, 'rb') as f:
            requests.post(f"{ORCHESTRATOR_URL}/upload_result", files={'file': f})
        
        print("SUCCESS: Job complete and results sent back")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    if setup_node():
        run_sandboxed_job()