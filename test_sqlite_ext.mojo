from std.memory import alloc

fn main() raises:
    var db_path = String("test.db")
    var ppDb = alloc[Pointer[None]](1)
    
    var rc = external_call["sqlite3_open", Int32](db_path.unsafe_ptr(), ppDb)
    print("sqlite3_open returned", rc)
    
    var db = ppDb.load(0)
    ppDb.free()
    
    var rc_close = external_call["sqlite3_close", Int32](db)
    print("sqlite3_close returned", rc_close)
