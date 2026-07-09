# ort_ffi.mojo
# Pure Mojo FFI bindings for ONNX Runtime via the ort_helper.c++ shim.
# Exposes three simple C-linkage functions:
#   init_ort_session(model_path) -> void*
#   run_ort_inference(session, input_ids, attention_mask, token_type_ids, seq_len, out_embedding) -> int
#   free_ort_session(session) -> void

from std.ffi import OwnedDLHandle
from std.memory import UnsafePointer, alloc
from std.collections import List

alias OrtPtr = UnsafePointer[UInt8, MutAnyOrigin]

fn string_to_cstr_buf(s: String) -> List[UInt8]:
    var buf = List[UInt8]()
    var p = s.unsafe_ptr()
    for i in range(len(s)):
        buf.append(p.load(i))
    buf.append(0)
    return buf^

struct OrtSession:
    """Wraps an ONNX Runtime inference session via libort_helper.so."""
    var _lib:     OwnedDLHandle
    var _session: OrtPtr
    var embedding_dim: Int

    fn __init__(out self: Self, lib_path: String, model_path: String, embedding_dim: Int = 512) raises:
        self._lib = OwnedDLHandle(lib_path)
        self.embedding_dim = embedding_dim
        var path_buf = string_to_cstr_buf(model_path)
        var session = self._lib.call["init_ort_session", OrtPtr](
            path_buf.unsafe_ptr().bitcast[UInt8]()
        )
        if not session:
            raise Error("init_ort_session failed for: " + model_path)
        self._session = session

    fn infer(self, input_ids: List[Int], attention_mask: List[Int]) raises -> List[Float32]:
        """Run inference and return the embedding vector.
        
        Args:
            input_ids: Token IDs (length = seq_len).
            attention_mask: 1 for real tokens, 0 for padding (length = seq_len).
        
        Returns:
            Embedding vector of length embedding_dim.
        """
        var seq_len = len(input_ids)
        if seq_len == 0:
            raise Error("input_ids is empty")
        if len(attention_mask) != seq_len:
            raise Error("attention_mask length mismatch")

        # Build Int64 buffers for input_ids and attention_mask
        var ids_buf   = alloc[Int64](seq_len)
        var mask_buf  = alloc[Int64](seq_len)
        var emb_buf   = alloc[Float32](self.embedding_dim)

        for i in range(seq_len):
            ids_buf.store(i, Int64(input_ids[i]))
            mask_buf.store(i, Int64(attention_mask[i]))

        var rc = self._lib.call["run_ort_inference", Int32](
            self._session,
            ids_buf.bitcast[UInt8](),
            mask_buf.bitcast[UInt8](),
            OrtPtr(),             # token_type_ids = null → will use zeros inside
            Int32(seq_len),
            emb_buf.bitcast[UInt8]()
        )

        var result = List[Float32]()
        if rc == 0:
            for i in range(self.embedding_dim):
                result.append(emb_buf.load(i))
        
        ids_buf.free()
        mask_buf.free()
        emb_buf.free()

        if rc != 0:
            raise Error("run_ort_inference failed: rc=" + String(rc))

        return result^

    fn close(self):
        if self._session:
            self._lib.call["free_ort_session", NoneType](self._session)
