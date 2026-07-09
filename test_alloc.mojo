from std.memory import alloc, free

fn main() raises:
    var p = alloc[Int8](6)
    p.store(0, 104)
    p.store(1, 101)
    p.store(2, 108)
    p.store(3, 108)
    p.store(4, 111)
    p.store(5, 0)
    # String from pointer
    # Let's see if String(p, 5) compiles
    var s = String(p, 5)
    print(s)
    free(p)
