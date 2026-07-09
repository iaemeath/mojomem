from std.memory import Pointer

fn main() raises:
    var p = Pointer[Int8].alloc(6)
    p.store(0, 104)
    p.store(1, 101)
    p.store(2, 108)
    p.store(3, 108)
    p.store(4, 111)
    p.store(5, 0)
    
    var s2 = String(p, 5)
    print(s2)
