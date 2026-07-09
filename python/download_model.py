import os
from huggingface_hub import snapshot_download

if __name__ == "__main__":
    os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
    print("Downloading ONNX model from hf-mirror...")
    snapshot_download(
        repo_id="Xenova/bge-small-zh-v1.5", 
        local_dir="bge-small-zh-v1.5-onnx", 
        local_dir_use_symlinks=False
    )
    print("✅ Download complete.")
