fn main() raises:
    var s = String("hello world")
    var sub = StringRef(s.unsafe_ptr() + 6, 5)
    print(sub)
