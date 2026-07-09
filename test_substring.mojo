fn substring(s: String, start: Int, end: Int) -> String:
    var res = String()
    for i in range(start, end):
        res += s[byte=i]
    return res

fn main() raises:
    var s = String("hello world")
    print(substring(s, 6, 11))
