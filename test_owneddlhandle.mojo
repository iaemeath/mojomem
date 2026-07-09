from std.ffi import OwnedDLHandle

fn main() raises:
    var handle = OwnedDLHandle("/usr/lib/x86_64-linux-gnu/libsqlite3.so.0")
    print("Loaded libsqlite3 successfully using OwnedDLHandle!")
