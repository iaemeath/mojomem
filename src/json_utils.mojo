# json_utils.mojo
# Minimal JSON builder and parser for the MCP server.

from std.collections import List

# ── JSON builder ─────────────────────────────────────────────────────────────
fn json_escape(s: String) -> String:
    var res = String()
    var ptr = s.unsafe_ptr()
    for i in range(len(s)):
        var b = Int(ptr.load(i))
        if b == 92:      # backslash
            res += "\\\\"
        elif b == 34:    # double-quote
            res += "\\\""
        elif b == 10:    # newline
            res += "\\n"
        elif b == 13:    # carriage return
            res += "\\r"
        elif b == 9:     # tab
            res += "\\t"
        elif b < 32:     # other control chars
            res += "\\u00"
            var hi = b >> 4
            var lo = b & 0xF
            if hi < 10: res += String(hi)
            else: res += String(Codepoint(unsafe_unchecked_codepoint=UInt32(hi - 10 + 97)))
            if lo < 10: res += String(lo)
            else: res += String(Codepoint(unsafe_unchecked_codepoint=UInt32(lo - 10 + 97)))
        else:
            res.append(Codepoint(unsafe_unchecked_codepoint=UInt32(b)))
    return res^

fn json_str(s: String) -> String:
    return '"' + json_escape(s) + '"'

fn json_kv(k: String, v: String) -> String:
    return json_str(k) + ": " + v

fn json_kv_str(k: String, v: String) -> String:
    return json_str(k) + ": " + json_str(v)

fn json_kv_int(k: String, v: Int) -> String:
    return json_str(k) + ": " + String(v)

fn json_kv_bool(k: String, v: Bool) -> String:
    if v: return json_str(k) + ": true"
    return json_str(k) + ": false"

fn json_obj(*pairs: String) -> String:
    var res = String("{")
    for i in range(len(pairs)):
        if i > 0: res += ", "
        res += pairs[i]
    res += "}"
    return res^

fn json_arr(items: List[String]) -> String:
    var res = String("[")
    for i in range(len(items)):
        if i > 0: res += ", "
        res += items[i]
    res += "]"
    return res^

# ── Minimal JSON parser ──────────────────────────────────────────────────────

fn _skip_whitespace(s: String, pos: Int) -> Int:
    var ptr = s.unsafe_ptr()
    var n = len(s)
    var i = pos
    while i < n:
        var b = ptr.load(i)
        if b != 32 and b != 9 and b != 10 and b != 13:
            return i
        i += 1
    return n

fn _find_key(s: String, key: String, start: Int) -> Int:
    var target = '"' + key + '"'
    var p = s.find(target, start)
    if p == -1: return -1
    var ptr = s.unsafe_ptr()
    var n = len(s)
    var i = p + len(target)
    i = _skip_whitespace(s, i)
    if i >= n or ptr.load(i) != 58: return -1  # ':'
    i += 1
    return _skip_whitespace(s, i)

fn _read_string_value(s: String, pos: Int) -> String:
    var ptr = s.unsafe_ptr()
    var n = len(s)
    if pos >= n or ptr.load(pos) != 34: return ""
    var i = pos + 1
    var res = String()
    while i < n:
        var b = Int(ptr.load(i))
        if b == 92 and i + 1 < n:
            var next_b = Int(ptr.load(i + 1))
            if next_b == 110:   res += "\n"
            elif next_b == 116: res += "\t"
            elif next_b == 114: res += "\r"
            elif next_b == 92:  res += "\\"
            elif next_b == 34:  res += '"'
            elif next_b == 47:  res += "/"
            else: res.append(Codepoint(unsafe_unchecked_codepoint=UInt32(next_b)))
            i += 2
        elif b == 34:
            return res^
        else:
            res.append(Codepoint(unsafe_unchecked_codepoint=UInt32(b)))
            i += 1
    return res^

fn _read_number_value(s: String, pos: Int) -> String:
    var ptr = s.unsafe_ptr()
    var n = len(s)
    var i = pos
    var res = String()
    while i < n:
        var b = Int(ptr.load(i))
        if b == 44 or b == 125 or b == 93 or b == 32 or b == 10: break
        res.append(Codepoint(unsafe_unchecked_codepoint=UInt32(b)))
        i += 1
    return res^

fn _read_object_value(s: String, pos: Int) -> String:
    """Read a JSON object or array string."""
    var ptr = s.unsafe_ptr()
    var n = len(s)
    if pos >= n: return ""
    var start_char = ptr.load(pos)
    if start_char != 123 and start_char != 91: return "" # { or [
    var closing_char: UInt8 = 125 # }
    if start_char == 91: closing_char = 93 # ]
    var depth = 0
    var i = pos
    var in_string = False
    var escape = False
    while i < n:
        var b = ptr.load(i)
        if not in_string:
            if b == 34: in_string = True
            elif b == start_char: depth += 1
            elif b == closing_char:
                depth -= 1
                if depth == 0:
                    return String(StringSlice(ptr=ptr + pos, length=i - pos + 1))
        else:
            if escape: escape = False
            elif b == 92: escape = True
            elif b == 34: in_string = False
        i += 1
    return ""

fn json_get_string(json: String, key: String, default_val: String = "") -> String:
    var pos = _find_key(json, key, 0)
    if pos == -1: return default_val
    var ptr = json.unsafe_ptr()
    if ptr.load(pos) == 34: return _read_string_value(json, pos)
    return default_val

fn json_get_int(json: String, key: String, default_val: Int = 0) -> Int:
    var pos = _find_key(json, key, 0)
    if pos == -1: return default_val
    var ptr = json.unsafe_ptr()
    if ptr.load(pos) == 34:
        try: return atol(_read_string_value(json, pos))
        except: return default_val
    var raw = _read_number_value(json, pos)
    if len(raw) == 0: return default_val
    var dot = raw.find(".")
    if dot != -1:
        try: return atol(String(StringSlice(ptr=raw.unsafe_ptr(), length=dot)))
        except: return default_val
    try: return atol(raw)
    except: return default_val

fn json_get_float(json: String, key: String, default_val: Float64 = 0.0) -> Float64:
    var pos = _find_key(json, key, 0)
    if pos == -1: return default_val
    var raw = _read_number_value(json, pos)
    if len(raw) == 0: return default_val
    var dot = raw.find(".")
    if dot == -1:
        try: return Float64(atol(raw))
        except: return default_val
    try:
        var int_part = atol(String(StringSlice(ptr=raw.unsafe_ptr(), length=dot)))
        var frac_str = String(StringSlice(ptr=raw.unsafe_ptr() + dot + 1, length=len(raw) - dot - 1))
        var frac_val: Float64 = 0.0
        var denom: Float64 = 10.0
        for idx in range(len(frac_str)):
            frac_val += Float64(Int(frac_str.unsafe_ptr().load(idx)) - 48) / denom
            denom *= 10.0
        if int_part < 0: return Float64(int_part) - frac_val
        return Float64(int_part) + frac_val
    except: return default_val

fn json_get_id(json: String) -> String:
    var pos = _find_key(json, "id", 0)
    if pos == -1: return "null"
    var ptr = json.unsafe_ptr()
    if pos < len(json) and ptr.load(pos) == 34:
        return json_str(_read_string_value(json, pos))
    var raw = _read_number_value(json, pos)
    if len(raw) == 0: return "null"
    return raw

fn json_get_obj(json: String, key: String) -> String:
    """Extract an object or array string."""
    var pos = _find_key(json, key, 0)
    if pos == -1: return ""
    return _read_object_value(json, pos)
