from std.collections import Dict

fn substring(s: String, start: Int, end: Int) -> String:
    var sub = StringSlice(ptr=s.unsafe_ptr() + start, length=end - start)
    return String(sub)

fn load_vocab(path: String) raises -> Dict[String, Int]:
    var vocab = Dict[String, Int]()
    var f = open(path, "r")
    var content = f.read()
    f.close()
    
    var vocab_start = content.find('"vocab": {')
    if vocab_start == -1:
        raise Error("Could not find 'vocab' section in tokenizer.json")
    
    var start_idx = content.find('{', vocab_start)
    if start_idx == -1:
        raise Error("Could not find opening brace for vocab")
        
    var ptr = content.unsafe_ptr()
    var n = len(content)
    
    var pos = start_idx + 1
    while pos < n:
        # Find next non-whitespace character
        var first_char_pos = pos
        while first_char_pos < n:
            var b = ptr.load(first_char_pos)
            if b != 32 and b != 9 and b != 10 and b != 13:
                break
            first_char_pos += 1
            
        if first_char_pos >= n:
            break
            
        var first_char = ptr.load(first_char_pos)
        if first_char == 125: # '}'
            # Reached end of vocab!
            break
            
        if first_char != 34: # '"'
            raise Error("Expected double quote or closing brace in vocab at pos " + String(first_char_pos))
            
        # Find ending double quote of the key
        var quote2 = first_char_pos + 1
        while quote2 < n:
            if ptr.load(quote2) == 34 and ptr.load(quote2 - 1) != 92:
                break
            quote2 += 1
        
        if quote2 >= n:
            break
            
        var key = substring(content, first_char_pos + 1, quote2)
        
        # Unescape key (basic handling of \" and \\)
        key = key.replace('\\"', '"').replace('\\\\', '\\')
        
        # Find colon
        var colon = quote2 + 1
        while colon < n:
            if ptr.load(colon) == 58:
                break
            colon += 1
            
        if colon >= n:
            break
            
        # Find start of number (skip spaces)
        var num_start = colon + 1
        while num_start < n:
            var b = ptr.load(num_start)
            if b != 32 and b != 9 and b != 10 and b != 13:
                break
            num_start += 1
            
        # Find end of number
        var num_end = num_start
        while num_end < n:
            var b = ptr.load(num_end)
            if b == 44 or b == 10 or b == 13 or b == 32 or b == 9 or b == 125: # ',' '\n' '\r' ' ' '\t' '}'
                break
            num_end += 1
            
        var val_str = substring(content, num_start, num_end)
        var val = atol(val_str)
        
        vocab[key] = val
        pos = num_end
        
        # Skip trailing comma if present
        var next_pos = pos
        while next_pos < n:
            var b = ptr.load(next_pos)
            if b == 44: # ','
                pos = next_pos + 1
                break
            elif b != 32 and b != 9 and b != 10 and b != 13:
                break
            next_pos += 1
        
    return vocab^

fn main() raises:
    print("Loading vocab from bge-small-zh-v1.5-onnx/tokenizer.json...")
    var vocab = load_vocab("bge-small-zh-v1.5-onnx/tokenizer.json")
    print("Vocab loaded! Size =", len(vocab))
    print("[PAD] id =", vocab.get("[PAD]", -1))
    print("[CLS] id =", vocab.get("[CLS]", -1))
    print("[SEP] id =", vocab.get("[SEP]", -1))
    print("你好 id =", vocab.get("你好", -1))
    print("code id =", vocab.get("code", -1))
