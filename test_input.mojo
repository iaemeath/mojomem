fn main() raises:
    print("Type something:")
    try:
        var line = input()
        print("You typed:", line)
    except e:
        print("Error reading input:", e)
