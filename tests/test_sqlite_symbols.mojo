from std.ffi import OwnedDLHandle
from std.memory import UnsafePointer

fn main() raises:
    var handle = OwnedDLHandle("/usr/lib/x86_64-linux-gnu/libsqlite3.so.0")
    
    var sqlite3_open = handle.get_symbol[fn(UnsafePointer[UInt8, _], UnsafePointer[UnsafePointer[UInt8, _], _]) -> Int32]("sqlite3_open")
    print("Successfully retrieved sqlite3_open!")
