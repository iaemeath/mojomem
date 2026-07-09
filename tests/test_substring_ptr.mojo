fn substring(s: String, start: Int, end: Int) -> String:
    var res = String()
    var ptr = s.unsafe_ptr()
    for i in range(start, end):
        res.append(Codepoint(Int(ptr.load(i))))
    return res

fn main() raises:
    var s = String("你好 world")
    print(substring(s, 0, 6))
