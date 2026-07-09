from std.memory import alloc, UnsafePointer

fn main() raises:
    # alloc[T](n) returns a pointer to n elements of type T on the heap
    var db_ptr = alloc[UnsafePointer[UInt8, MutAnyOrigin]](1)
    db_ptr.store(UnsafePointer[UInt8, MutAnyOrigin]())
    
    print("Alloc OK!")
    var val = db_ptr.load()
    print("Val null:", not val)
    print("OK!")
