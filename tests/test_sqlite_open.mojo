from std.sys.ffi import DLHandle

fn main() raises:
    var handle = DLHandle("/usr/lib/x86_64-linux-gnu/libsqlite3.so.0")
    print("Loaded libsqlite3 successfully!")
