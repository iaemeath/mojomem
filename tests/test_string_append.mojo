fn main() raises:
    var s = String("hello")
    var sub = String()
    sub += s[byte=0]
    sub += s[byte=1]
    print(sub)
