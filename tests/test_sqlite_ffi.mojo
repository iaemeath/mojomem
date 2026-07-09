# test_sqlite_ffi.mojo — verify the SQLite FFI module works end-to-end
from sqlite_ffi import SQLiteDB, SQLITE_ROW

fn main() raises:
    print("=== SQLite FFI Test ===")

    var db = SQLiteDB(
        "/home/iaemeath/code/mojomem/libmj_sqlite.so",
        "/tmp/test_mojo_sqlite.db"
    )
    print("✓ Database opened")

    db.exec("DROP TABLE IF EXISTS items")
    db.exec("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, score REAL)")
    print("✓ Table created")

    var stmt = db.prepare("INSERT INTO items (name, score) VALUES (?, ?)")
    db.bind_text(stmt, 1, "Mojo Language")
    db.bind_double(stmt, 2, 9.5)
    _ = db.step(stmt)
    db.finalize(stmt)
    var row1_id = db.last_insert_rowid()
    print("✓ Inserted row id =", row1_id)

    stmt = db.prepare("INSERT INTO items (name, score) VALUES (?, ?)")
    db.bind_text(stmt, 1, "Pure FFI")
    db.bind_double(stmt, 2, 8.8)
    _ = db.step(stmt)
    db.finalize(stmt)
    var row2_id = db.last_insert_rowid()
    print("✓ Inserted row id =", row2_id)

    print("\n--- Query results (ORDER BY score DESC) ---")
    stmt = db.prepare("SELECT id, name, score FROM items ORDER BY score DESC")
    var rc = db.step(stmt)
    while rc == SQLITE_ROW:
        var id    = db.col_int64(stmt, 0)
        var name  = db.col_text(stmt, 1)
        var score = db.col_double(stmt, 2)
        print("  id=" + String(id) + "  name=" + name + "  score=" + String(score))
        rc = db.step(stmt)
    db.finalize(stmt)

    db.exec("DROP TABLE IF EXISTS items")
    db.close()
    print("\n✓ All SQLite FFI tests PASSED!")
