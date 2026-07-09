import urllib.request
import tarfile
import zipfile
import os

def download_and_extract():
    os.makedirs("ort_sdk", exist_ok=True)
    
    # Download Linux tgz for WSL compiling
    linux_url = "https://github.com/microsoft/onnxruntime/releases/download/v1.18.1/onnxruntime-linux-x64-1.18.1.tgz"
    linux_tgz = "onnxruntime-linux-x64-1.18.1.tgz"
    
    print("Downloading ONNX Runtime Linux x64 SDK...")
    if not os.path.exists(linux_tgz):
        urllib.request.urlretrieve(linux_url, linux_tgz)
        print("Downloaded Linux SDK.")
    else:
        print("Linux SDK tgz already exists.")
        
    print("Extracting Linux SDK...")
    with tarfile.open(linux_tgz, "r:gz") as tar:
        tar.extractall("ort_sdk")
    print("Extracted Linux SDK.")
    
    # Rename extracted directory to 'linux'
    extracted_dir = os.path.join("ort_sdk", "onnxruntime-linux-x64-1.18.1")
    target_dir = os.path.join("ort_sdk", "linux")
    if os.path.exists(extracted_dir):
        if os.path.exists(target_dir):
            import shutil
            shutil.rmtree(target_dir)
        os.rename(extracted_dir, target_dir)
    print("Linux SDK setup complete in ort_sdk/linux.")

if __name__ == "__main__":
    download_and_extract()
