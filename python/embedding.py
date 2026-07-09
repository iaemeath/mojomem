import os
import sys
import numpy as np
try:
    import onnxruntime as ort
    from tokenizers import Tokenizer
    HAS_ONNX = True
except ImportError:
    HAS_ONNX = False

class BGEEmbedding:
    def __init__(self, model_dir='bge-small-zh-v1.5-onnx'):
        self.model_dir = model_dir
        self.session = None
        self.tokenizer = None
        self._load_model()

    def _load_model(self):
        if not HAS_ONNX:
            print("WARNING: onnxruntime/tokenizers not installed, mocking embeddings", file=sys.stderr)
            return
        onnx_path = os.path.join(self.model_dir, "onnx", "model_uint8.onnx")
        if not os.path.exists(onnx_path):
            onnx_path = os.path.join(self.model_dir, "onnx", "model.onnx")
        tok_path = os.path.join(self.model_dir, "tokenizer.json")
        if os.path.exists(onnx_path) and os.path.exists(tok_path):
            print(f'Loading model: {onnx_path}', file=sys.stderr)
            self.session = ort.InferenceSession(onnx_path, providers=['CPUExecutionProvider'])
            self.tokenizer = Tokenizer.from_file(tok_path)
            self.tokenizer.enable_truncation(max_length=510)  # 留 2 给 [CLS][SEP]，防 position 越界
            print(f"Inputs: {[i.name for i in self.session.get_inputs()]}", file=sys.stderr)
            print(f"Outputs: {[o.name for o in self.session.get_outputs()]}", file=sys.stderr)
        else:
            print(f"WARNING: Model not found at {onnx_path}", file=sys.stderr)

    def embed(self, text):
        if not HAS_ONNX or not self.session or not self.tokenizer:
            return [0.0] * 512
        encoded = self.tokenizer.encode(text)
        inputs = {
            "input_ids": [encoded.ids],
            "attention_mask": [encoded.attention_mask],
        }
        input_names = [i.name for i in self.session.get_inputs()]
        if "token_type_ids" in input_names:
            inputs["token_type_ids"] = [encoded.type_ids]
        outputs = self.session.run(None, inputs)
        last_hidden = outputs[0]
        mask = np.array(encoded.attention_mask, dtype=np.float32)
        mask = np.expand_dims(mask, axis=(0, -1)) # shape (1, seq_len, 1)
        masked = last_hidden * mask
        summed = masked.sum(axis=1)
        counts = mask.sum(axis=1).clip(min=1e-9)
        mean_pooled = summed / counts
        norm = np.linalg.norm(mean_pooled, axis=1, keepdims=True).clip(min=1e-9)
        return (mean_pooled / norm).flatten().tolist()

if __name__ == "__main__":
    e = BGEEmbedding()
    v = e.embed("test")
    print(f"dim={len(v)}")
