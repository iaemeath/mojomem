from std.memory import UnsafePointer

fn main():
    # In Mojo 0.26, to get a pointer to a local variable,
    # use UnsafePointer[T].alloc(1), store there, then pass that ptr
    var db_ptr = UnsafePointer[UnsafePointer[UInt8, MutAnyOrigin]].alloc(1)
    db_ptr.store(UnsafePointer[UInt8, MutAnyOrigin]())
    
    print("db_ptr:", db_ptr.__bool__())
    
    # Read back
    var val = db_ptr.load()
    print("val null:", not val)
    
    db_ptr.free()
    print("OK!")
