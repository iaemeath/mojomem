# sqlite_ffi.mojo
# Pure Mojo FFI bindings for SQLite3 via the mj_sqlite.c shim.
# Uses OwnedDLHandle.call[name, ReturnType](*args) directly —
# no function pointer caching, no .load() needed.

from std.ffi import OwnedDLHandle
from std.memory import UnsafePointer
from std.collections import List

comptime SQLITE_OK   = 0
comptime SQLITE_ROW  = 100
comptime SQLITE_DONE = 101

# Opaque byte-pointer type for db/stmt handles and string buffers
alias DbPtr = UnsafePointer[UInt8, MutAnyOrigin]

# ── helpers ──────────────────────────────────────────────────────────────────
fn cstr_to_string(ptr: DbPtr) -> String:
    if not ptr:
        return ""
    var length = 0
    while ptr.load(length) != 0:
        length += 1
    return String(StringSlice(ptr=ptr, length=length))

fn string_to_cstr_buf(s: String) -> List[UInt8]:
    var buf = List[UInt8]()
    var p = s.unsafe_ptr()
    for i in range(len(s)):
        buf.append(p.load(i))
    buf.append(0)
    return buf^

# ── SQLiteDB struct ───────────────────────────────────────────────────────────
struct SQLiteDB:
    """Native SQLite3 connection via mj_sqlite C shim (void* handles).
    
    Calls functions directly via OwnedDLHandle.call[name, ReturnType]()
    — avoids both double-pointer and .load() issues in Mojo 0.26.
    """
    var _lib: OwnedDLHandle
    var _db:  DbPtr

    fn __init__(out self: Self, lib_path: String, db_path: String) raises:
        self._lib = OwnedDLHandle(lib_path)
        var path_buf = string_to_cstr_buf(db_path)
        var db = self._lib.call["mj_sqlite3_open", DbPtr](
            path_buf.unsafe_ptr().bitcast[UInt8]()
        )
        if not db:
            raise Error("mj_sqlite3_open failed for: " + db_path)
        self._db = db

    fn errmsg(self) -> String:
        var ptr = self._lib.call["mj_sqlite3_errmsg", DbPtr](self._db)
        return cstr_to_string(ptr)

    fn exec(self, sql: String) raises:
        var sql_buf = string_to_cstr_buf(sql)
        var rc = self._lib.call["mj_sqlite3_exec", Int32](
            self._db, sql_buf.unsafe_ptr().bitcast[UInt8]()
        )
        if rc != SQLITE_OK:
            raise Error("exec failed (" + String(rc) + "): " + self.errmsg())

    fn prepare(self, sql: String) raises -> DbPtr:
        var sql_buf = string_to_cstr_buf(sql)
        var stmt = self._lib.call["mj_sqlite3_prepare", DbPtr](
            self._db, sql_buf.unsafe_ptr().bitcast[UInt8]()
        )
        if not stmt:
            raise Error("prepare failed: " + self.errmsg())
        return stmt

    fn step(self, stmt: DbPtr) -> Int32:
        return self._lib.call["mj_sqlite3_step", Int32](stmt)

    fn reset(self, stmt: DbPtr):
        _ = self._lib.call["mj_sqlite3_reset", Int32](stmt)

    fn finalize(self, stmt: DbPtr):
        _ = self._lib.call["mj_sqlite3_finalize", Int32](stmt)

    fn bind_text(self, stmt: DbPtr, col: Int, val: String) raises:
        var buf = string_to_cstr_buf(val)
        var rc = self._lib.call["mj_sqlite3_bind_text", Int32](
            stmt, Int32(col), buf.unsafe_ptr().bitcast[UInt8](), Int32(len(val))
        )
        if rc != SQLITE_OK:
            raise Error("bind_text failed col=" + String(col))

    fn bind_int64(self, stmt: DbPtr, col: Int, val: Int64) raises:
        var rc = self._lib.call["mj_sqlite3_bind_int64", Int32](
            stmt, Int32(col), val
        )
        if rc != SQLITE_OK:
            raise Error("bind_int64 failed col=" + String(col))

    fn bind_double(self, stmt: DbPtr, col: Int, val: Float64) raises:
        var rc = self._lib.call["mj_sqlite3_bind_double", Int32](
            stmt, Int32(col), val
        )
        if rc != SQLITE_OK:
            raise Error("bind_double failed col=" + String(col))

    fn bind_null(self, stmt: DbPtr, col: Int) raises:
        var rc = self._lib.call["mj_sqlite3_bind_null", Int32](stmt, Int32(col))
        if rc != SQLITE_OK:
            raise Error("bind_null failed col=" + String(col))

    fn bind_blob(self, stmt: DbPtr, col: Int, data: DbPtr, length: Int) raises:
        var rc = self._lib.call["mj_sqlite3_bind_blob", Int32](
            stmt, Int32(col), data, Int32(length)
        )
        if rc != SQLITE_OK:
            raise Error("bind_blob failed col=" + String(col))

    fn col_text(self, stmt: DbPtr, col: Int) -> String:
        var ptr = self._lib.call["mj_sqlite3_column_text", DbPtr](stmt, Int32(col))
        if not ptr:
            return ""
        var length = Int(self._lib.call["mj_sqlite3_column_bytes", Int32](stmt, Int32(col)))
        return String(StringSlice(ptr=ptr, length=length))

    fn col_int64(self, stmt: DbPtr, col: Int) -> Int64:
        return self._lib.call["mj_sqlite3_column_int64", Int64](stmt, Int32(col))

    fn col_double(self, stmt: DbPtr, col: Int) -> Float64:
        return self._lib.call["mj_sqlite3_column_double", Float64](stmt, Int32(col))

    fn col_type(self, stmt: DbPtr, col: Int) -> Int32:
        return self._lib.call["mj_sqlite3_column_type", Int32](stmt, Int32(col))

    fn col_count(self, stmt: DbPtr) -> Int32:
        return self._lib.call["mj_sqlite3_column_count", Int32](stmt)

    fn col_bytes(self, stmt: DbPtr, col: Int) -> Int32:
        return self._lib.call["mj_sqlite3_column_bytes", Int32](stmt, Int32(col))

    fn col_blob(self, stmt: DbPtr, col: Int) -> DbPtr:
        return self._lib.call["mj_sqlite3_column_blob", DbPtr](stmt, Int32(col))

    fn last_insert_rowid(self) -> Int64:
        return self._lib.call["mj_sqlite3_last_insert_rowid", Int64](self._db)

    fn enable_load_extension(self) raises:
        var rc = self._lib.call["mj_sqlite3_enable_load_extension", Int32](self._db, Int32(1))
        if rc != SQLITE_OK:
            raise Error("enable_load_extension failed")

    fn load_extension(self, ext_path: String) raises:
        var path_buf = string_to_cstr_buf(ext_path)
        var rc = self._lib.call["mj_sqlite3_load_extension", Int32](
            self._db, path_buf.unsafe_ptr().bitcast[UInt8]()
        )
        if rc != SQLITE_OK:
            raise Error("load_extension failed for: " + ext_path)

    fn close(self):
        if self._db:
            self._lib.call["mj_sqlite3_close", NoneType](self._db)
