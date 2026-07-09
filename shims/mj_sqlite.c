#include <sqlite3.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

// ============================================================
// Thin C wrappers to avoid double-pointer (sqlite3**) FFI issues
// from Mojo. All functions use simple scalar types and single
// indirection so they are trivially bindable via OwnedDLHandle.
// ============================================================

// Open DB and return the sqlite3* opaque handle as void*.
// Returns NULL on failure.
EXPORT void* mj_sqlite3_open(const char* path) {
    sqlite3* db = NULL;
    int rc = sqlite3_open_v2(path, &db,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, NULL);
    if (rc != SQLITE_OK) {
        if (db) sqlite3_close(db);
        return NULL;
    }
    return (void*)db;
}

EXPORT void mj_sqlite3_close(void* db) {
    if (db) sqlite3_close((sqlite3*)db);
}

// Execute a no-result SQL statement.
// Returns 0=OK, non-zero=error.
EXPORT int mj_sqlite3_exec(void* db, const char* sql) {
    char* errmsg = NULL;
    int rc = sqlite3_exec((sqlite3*)db, sql, NULL, NULL, &errmsg);
    if (errmsg) sqlite3_free(errmsg);
    return rc;
}

// Prepare a statement, returns void* stmt (or NULL on error).
EXPORT void* mj_sqlite3_prepare(void* db, const char* sql) {
    sqlite3_stmt* stmt = NULL;
    int rc = sqlite3_prepare_v2((sqlite3*)db, sql, -1, &stmt, NULL);
    if (rc != SQLITE_OK) return NULL;
    return (void*)stmt;
}

EXPORT int mj_sqlite3_step(void* stmt) {
    return sqlite3_step((sqlite3_stmt*)stmt);
}

EXPORT int mj_sqlite3_reset(void* stmt) {
    return sqlite3_reset((sqlite3_stmt*)stmt);
}

EXPORT int mj_sqlite3_finalize(void* stmt) {
    return sqlite3_finalize((sqlite3_stmt*)stmt);
}

// Bind text - copies text (SQLITE_TRANSIENT).
EXPORT int mj_sqlite3_bind_text(void* stmt, int col, const char* text, int len) {
    return sqlite3_bind_text((sqlite3_stmt*)stmt, col, text, len, SQLITE_TRANSIENT);
}

EXPORT int mj_sqlite3_bind_int64(void* stmt, int col, long long val) {
    return sqlite3_bind_int64((sqlite3_stmt*)stmt, col, (sqlite3_int64)val);
}

EXPORT int mj_sqlite3_bind_double(void* stmt, int col, double val) {
    return sqlite3_bind_double((sqlite3_stmt*)stmt, col, val);
}

EXPORT int mj_sqlite3_bind_null(void* stmt, int col) {
    return sqlite3_bind_null((sqlite3_stmt*)stmt, col);
}

EXPORT int mj_sqlite3_bind_blob(void* stmt, int col, const void* data, int len) {
    return sqlite3_bind_blob((sqlite3_stmt*)stmt, col, data, len, SQLITE_TRANSIENT);
}

// Column accessors. text is returned as const char* (owned by SQLite until next step).
EXPORT const char* mj_sqlite3_column_text(void* stmt, int col) {
    return (const char*)sqlite3_column_text((sqlite3_stmt*)stmt, col);
}

EXPORT long long mj_sqlite3_column_int64(void* stmt, int col) {
    return (long long)sqlite3_column_int64((sqlite3_stmt*)stmt, col);
}

EXPORT double mj_sqlite3_column_double(void* stmt, int col) {
    return sqlite3_column_double((sqlite3_stmt*)stmt, col);
}

EXPORT int mj_sqlite3_column_type(void* stmt, int col) {
    return sqlite3_column_type((sqlite3_stmt*)stmt, col);
}

EXPORT int mj_sqlite3_column_count(void* stmt) {
    return sqlite3_column_count((sqlite3_stmt*)stmt);
}

EXPORT int mj_sqlite3_column_bytes(void* stmt, int col) {
    return sqlite3_column_bytes((sqlite3_stmt*)stmt, col);
}

EXPORT const void* mj_sqlite3_column_blob(void* stmt, int col) {
    return sqlite3_column_blob((sqlite3_stmt*)stmt, col);
}

EXPORT long long mj_sqlite3_last_insert_rowid(void* db) {
    return (long long)sqlite3_last_insert_rowid((sqlite3*)db);
}

EXPORT const char* mj_sqlite3_errmsg(void* db) {
    return sqlite3_errmsg((sqlite3*)db);
}

// Extension loading helpers (needed for sqlite-vec).
EXPORT int mj_sqlite3_enable_load_extension(void* db, int onoff) {
    return sqlite3_enable_load_extension((sqlite3*)db, onoff);
}

EXPORT int mj_sqlite3_load_extension(void* db, const char* path) {
    char* errmsg = NULL;
    int rc = sqlite3_load_extension((sqlite3*)db, path, NULL, &errmsg);
    if (errmsg) sqlite3_free(errmsg);
    return rc;
}
