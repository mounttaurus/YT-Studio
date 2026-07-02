FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI /comfyui

WORKDIR /comfyui

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8188

# VRAM 4GB環境のため --lowvram で起動
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--lowvram"]
