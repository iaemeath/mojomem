from std.collections import List

fn is_chinese_char(cp: Int) -> Bool:
    if (cp >= 0x4E00 and cp <= 0x9FFF) or \
       (cp >= 0x3400 and cp <= 0x4DBF) or \
       (cp >= 0x20000 and cp <= 0x2A6DF) or \
       (cp >= 0xF900 and cp <= 0xFAFF) or \
       (cp >= 0x2F800 and cp <= 0x2FA1F):
        return True
    return False

fn utf8_decode(s: String) -> List[Int]:
    var res = List[Int]()
    var ptr = s.unsafe_ptr()
    var n = len(s)
    var i = 0
    while i < n:
        var b1 = Int(ptr.load(i))
        if b1 < 128:
            res.append(b1)
            i += 1
        elif (b1 & 0xE0) == 0xC0:
            if i + 1 < n:
                var b2 = Int(ptr.load(i + 1))
                var cp = ((b1 & 0x1F) << 6) | (b2 & 0x3F)
                res.append(cp)
            i += 2
        elif (b1 & 0xF0) == 0xE0:
            if i + 2 < n:
                var b2 = Int(ptr.load(i + 1))
                var b3 = Int(ptr.load(i + 2))
                var cp = ((b1 & 0x0F) << 12) | ((b2 & 0x3F) << 6) | (b3 & 0x3F)
                res.append(cp)
            i += 3
        elif (b1 & 0xF8) == 0xF0:
            if i + 3 < n:
                var b2 = Int(ptr.load(i + 1))
                var b3 = Int(ptr.load(i + 2))
                var b4 = Int(ptr.load(i + 3))
                var cp = ((b1 & 0x07) << 18) | ((b2 & 0x3F) << 12) | ((b3 & 0x3F) << 6) | (b4 & 0x3F)
                res.append(cp)
            i += 4
        else:
            i += 1
    return res^

fn normalize_chinese(s: String) -> String:
    var cps = utf8_decode(s)
    var norm_cps = List[Int]()
    for idx in range(len(cps)):
        var cp = cps[idx]
        if is_chinese_char(cp):
            if len(norm_cps) > 0 and norm_cps[len(norm_cps) - 1] != 32:
                norm_cps.append(32)
            norm_cps.append(cp)
            norm_cps.append(32)
        else:
            if cp == 32 or cp == 9 or cp == 10 or cp == 13:
                if len(norm_cps) > 0 and norm_cps[len(norm_cps) - 1] != 32:
                    norm_cps.append(32)
            else:
                norm_cps.append(cp)
                
    var res = String()
    for idx in range(len(norm_cps)):
        res.append(Codepoint(unsafe_unchecked_codepoint=UInt32(norm_cps[idx])))
    return res^

fn main() raises:
    var s = String("我爱NLP")
    var norm = normalize_chinese(s)
    print("Original:", s)
    print("Normalized:", norm)
