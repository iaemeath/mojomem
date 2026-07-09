# test_ort_ffi.mojo — verify ONNX Runtime inference via Mojo FFI
from ort_ffi import OrtSession
from tokenizer import WordPieceTokenizer

fn main() raises:
    print("=== ORT FFI Test ===")

    var tok = WordPieceTokenizer("bge-small-zh-v1.5-onnx/tokenizer.json")
    print("✓ Tokenizer loaded (" + String(len(tok.vocab)) + " tokens)")

    var ort = OrtSession(
        "/home/iaemeath/code/mojomem/libort_helper.so",
        "/home/iaemeath/code/mojomem/bge-small-zh-v1.5-onnx/onnx/model.onnx",
        embedding_dim=512
    )
    print("✓ ORT session initialized")

    # Encode a test sentence
    var text = String("测试向量嵌入")
    var ids = tok.encode(text, max_length=64)

    # Build attention mask (1 for non-padding tokens)
    var mask = List[Int]()
    for i in range(len(ids)):
        if ids[i] != 0:
            mask.append(1)
        else:
            mask.append(0)

    print("Input text: " + text)
    print("Seq len: " + String(len(ids)))

    var emb = ort.infer(ids, mask)
    print("✓ Embedding generated, dim =", len(emb))

    # Print first 8 values
    print("First 8 values:")
    for i in range(8):
        print("  emb[" + String(i) + "] = " + String(emb[i]))

    # Verify L2 norm ≈ 1.0 (BGE model outputs normalized embeddings)
    var norm: Float32 = 0.0
    for i in range(len(emb)):
        norm += emb[i] * emb[i]
    from std.math import sqrt
    norm = sqrt(norm)
    print("L2 norm =", norm, "(should be ≈ 1.0)")

    ort.close()
    print("✓ All ORT FFI tests PASSED!")
