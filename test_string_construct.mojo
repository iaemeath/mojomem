from std.collections import List
from std.memory import Pointer

fn main() raises:
    # Let's test if we can construct a String from List[Int8]
    var bytes = List[Int8]()
    bytes.append(104) # 'h'
    bytes.append(101) # 'e'
    bytes.append(108) # 'l'
    bytes.append(108) # 'l'
    bytes.append(111) # 'o'
    bytes.append(0)   # Null terminator
    
    # Try constructing String
    var s = String(bytes)
    print(s)
