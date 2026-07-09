from std.collections import Dict
from std.collections import List

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
            break
            
        if first_char != 34: # '"'
            raise Error("Expected double quote or closing brace in vocab at pos " + String(first_char_pos))
            
        var quote2 = first_char_pos + 1
        while quote2 < n:
            if ptr.load(quote2) == 34 and ptr.load(quote2 - 1) != 92:
                break
            quote2 += 1
        
        if quote2 >= n:
            break
            
        var key = substring(content, first_char_pos + 1, quote2)
        key = key.replace('\\"', '"').replace('\\\\', '\\')
        
        var colon = quote2 + 1
        while colon < n:
            if ptr.load(colon) == 58:
                break
            colon += 1
            
        if colon >= n:
            break
            
        var num_start = colon + 1
        while num_start < n:
            var b = ptr.load(num_start)
            if b != 32 and b != 9 and b != 10 and b != 13:
                break
            num_start += 1
            
        var num_end = num_start
        while num_end < n:
            var b = ptr.load(num_end)
            if b == 44 or b == 10 or b == 13 or b == 32 or b == 9 or b == 125:
                break
            num_end += 1
            
        var val_str = substring(content, num_start, num_end)
        var val = atol(val_str)
        
        vocab[key] = val
        pos = num_end
        
        var next_pos = pos
        while next_pos < n:
            var b = ptr.load(next_pos)
            if b == 44:
                pos = next_pos + 1
                break
            elif b != 32 and b != 9 and b != 10 and b != 13:
                break
            next_pos += 1
        
    return vocab^

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
            if b1 >= 65 and b1 <= 90:
                b1 += 32
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

struct WordPieceTokenizer:
    var vocab: Dict[String, Int]
    var unk_id: Int
    var cls_id: Int
    var sep_id: Int
    var pad_id: Int
    
    fn __init__(out self: Self, vocab_path: String) raises:
        self.vocab = load_vocab(vocab_path)
        self.unk_id = self.vocab.get("[UNK]", 100)
        self.cls_id = self.vocab.get("[CLS]", 101)
        self.sep_id = self.vocab.get("[SEP]", 102)
        self.pad_id = self.vocab.get("[PAD]", 0)
        
    fn tokenize_word(self, word: String) -> List[Int]:
        var res = List[Int]()
        if len(word) == 0:
            return res^
            
        if word in self.vocab:
            res.append(self.vocab.get(word, self.unk_id))
            return res^
            
        var cps = utf8_decode(word)
        var start = 0
        var n = len(cps)
        while start < n:
            var end = n
            var cur_id = -1
            while start < end:
                var slice_cps = List[Int]()
                if start > 0:
                    slice_cps.append(35) # '#'
                    slice_cps.append(35) # '#'
                for k in range(start, end):
                    slice_cps.append(cps[k])
                    
                var substr = String()
                for idx in range(len(slice_cps)):
                    substr.append(Codepoint(unsafe_unchecked_codepoint=UInt32(slice_cps[idx])))
                    
                if substr in self.vocab:
                    cur_id = self.vocab.get(substr, self.unk_id)
                    break
                end -= 1
                
            if cur_id != -1:
                res.append(cur_id)
                start = end
            else:
                res.clear()
                res.append(self.unk_id)
                break
                
        return res^

    fn encode(self, text: String, max_length: Int = 512) -> List[Int]:
        var norm = normalize_chinese(text)
        
        var norm_cps = utf8_decode(norm)
        var words = List[String]()
        var current_word_cps = List[Int]()
        
        for idx in range(len(norm_cps)):
            var cp = norm_cps[idx]
            if cp == 32 or cp == 9 or cp == 10 or cp == 13:
                if len(current_word_cps) > 0:
                    var w = String()
                    for w_idx in range(len(current_word_cps)):
                        w.append(Codepoint(unsafe_unchecked_codepoint=UInt32(current_word_cps[w_idx])))
                    words.append(w)
                    current_word_cps.clear()
            else:
                current_word_cps.append(cp)
                
        if len(current_word_cps) > 0:
            var w = String()
            for w_idx in range(len(current_word_cps)):
                w.append(Codepoint(unsafe_unchecked_codepoint=UInt32(current_word_cps[w_idx])))
            words.append(w)
            
        var token_ids = List[Int]()
        token_ids.append(self.cls_id)
        
        for w_idx in range(len(words)):
            var word_tokens = self.tokenize_word(words[w_idx])
            for t_idx in range(len(word_tokens)):
                token_ids.append(word_tokens[t_idx])
                
        if len(token_ids) > max_length - 1:
            var truncated = List[Int]()
            for i in range(max_length - 1):
                truncated.append(token_ids[i])
            token_ids = truncated^
            
        token_ids.append(self.sep_id)
        
        while len(token_ids) < max_length:
            token_ids.append(self.pad_id)
            
        return token_ids^
