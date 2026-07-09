fn main() raises:
    var s = String("hello world")
    # unsafe_ptr() returns a pointer. Let's see if we can construct StringSlice from it
    var sub = StringSlice(s.unsafe_ptr() + 6, 5)
    print(String(sub))
