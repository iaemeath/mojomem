from json_utils import json_get_string, json_get_int, json_get_float, json_get_id, json_obj, json_kv_str, json_arr, json_str, json_get_obj

fn main() raises:
    var json = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    print("Method:", json_get_string(json, "method"))
    print("Id:", json_get_id(json))
    
    var args = json_get_obj(json, "arguments")
    print("Args:", args)
    if args:
        print("project_id:", json_get_string(args, "project_id"))
        print("content:", json_get_string(args, "content"))
        
    var obj = json_obj(
        json_kv_str("jsonrpc", "2.0"),
        '"id": ' + json_get_id(json),
        json_kv_str("result", "ok")
    )
    print("Built obj:", obj)
