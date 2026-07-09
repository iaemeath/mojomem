fn main() raises:
    var s = String("hello world")
    var sub = StringSlice(ptr=s.unsafe_ptr() + 6, length=5)
    print(String(sub))
