from std.memory import alloc

fn main() raises:
    var p = alloc[Int8](6)
    p.free()
    print("p.free() succeeded!")
