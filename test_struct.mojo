struct Foo:
    var x: Int
    fn __init__(out self: Self, x: Int):
        self.x = x

fn main():
    var f = Foo(10)
    print(f.x)
