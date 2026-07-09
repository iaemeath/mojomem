from std.memory import UnsafePointer

fn main():
    var x: Int32 = 42
    var ptr = __mlir_op.`pop.address_of`(x)
    print("got ptr")
