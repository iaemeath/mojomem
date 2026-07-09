from std.memory import alloc, deallocate

fn main() raises:
    var p = alloc[Int8](6)
    deallocate(p)
    print("Success!")
