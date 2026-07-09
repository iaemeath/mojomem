fn main() raises:
    var s = String("hello")
    var ptr = s.unsafe_ptr()
    print(ptr.load(0))
    print(ptr.load(1))
